"""内部试用：查询身份、反馈隔离与上下文回归。"""
from __future__ import annotations

from types import SimpleNamespace

import mongomock
import pytest

from app.evolution import repositories as evolution_module
from app.evolution.feedback.collector import record_feedback
from app.evolution.models import FeedbackEvent
from app.process.query.agent.state import create_query_default_state
from app.rag.query import chitchat_service, history_utils, intent_route_service
from app.shared.clients import history_repository as history_module
from app.shared.config import settings


@pytest.fixture
def db(monkeypatch):
    database = mongomock.MongoClient().trial
    monkeypatch.setattr(history_module, "get_collection", lambda name: database[name])
    monkeypatch.setattr(evolution_module, "get_collection", lambda name: database[name])
    monkeypatch.setattr(settings.evolution, "enabled", True)
    return database


def test_unknown_groundedness_survives_storage(db):
    repo = history_module.history_repository
    repo.save_message(session_id="s", role="assistant", text="hello", groundedness=None)
    assert repo.list_recent("s")[0]["groundedness"] is None
    repo.save_message(session_id="s", role="assistant", text="zero", groundedness=0.0)
    assert repo.list_recent("s")[0]["groundedness"] == 0.0


def test_product_history_survives_many_chitchat_rounds(db):
    repo = history_module.history_repository
    repo.save_message(session_id="s", role="user", text="HAK180", rewritten_query="HAK180怎么安装", item_names=["HAK180"], intent="rag")
    for i in range(12):
        repo.save_message(session_id="s", role="user", text=str(i), item_names=[], intent="chitchat")
    assert "HAK180" in history_utils.build_history_context("s", 6)


def test_chitchat_persists_user_before_first_delta(db, monkeypatch):
    repo = history_module.history_repository
    repo.save_message(session_id="s", role="assistant", text="previous")
    observations = []
    class Client:
        def stream(self, _messages):
            observations.append(repo.list_recent("s")[0])
            yield SimpleNamespace(content="你好")
    monkeypatch.setattr(chitchat_service.llm_providers, "chat", lambda **kw: Client())
    monkeypatch.setattr(chitchat_service, "publish", lambda *a: None)
    state = create_query_default_state(session_id="s", original_query="你好", is_stream=True, turn_id="turn-1")
    chitchat_service.generate_chitchat_answer(state)
    assert observations[0]["role"] == "user"
    assert observations[0]["turn_id"] == "turn-1"
    assert state["answer_id"]
    assert len(repo.list_recent("s")) == 3


@pytest.mark.parametrize("thumbs", [-1, 1])
def test_chitchat_feedback_never_becomes_knowledge(db, thumbs):
    repo = history_module.history_repository
    answer_id = repo.save_message(session_id="s", role="assistant", text="你好", intent="chitchat")
    record_feedback(FeedbackEvent(session_id="s", answer_id=answer_id, query="伪造产品问题", thumbs=thumbs))
    assert db[settings.mongo.fb_events_collection].count_documents({}) == 0


def test_feedback_uses_saved_rag_answer_not_client_claims(db):
    repo = history_module.history_repository
    answer_id = repo.save_message(session_id="s", role="assistant", text="answer", rewritten_query="HAK180安装", item_names=["HAK180"], intent="rag", citations=[{"faq_id": "chunk-1", "source": "kb"}])
    record_feedback(FeedbackEvent(session_id="s", answer_id=answer_id, query="tampered", item_names=["wrong"], cited_chunk_ids=["wrong"], thumbs=-1))
    event = db[settings.mongo.fb_events_collection].find_one()
    assert event["query"] == "HAK180安装"
    assert event["item_names"] == ["HAK180"]
    assert event["cited_chunk_ids"] == ["chunk-1"]


@pytest.mark.parametrize("answer_id", ["", "bad-id", "0" * 24])
def test_unverifiable_feedback_does_not_generate_knowledge(db, answer_id):
    record_feedback(FeedbackEvent(session_id="s", answer_id=answer_id, thumbs=-1))
    assert db[settings.mongo.fb_events_collection].count_documents({}) == 0


def test_cross_session_feedback_is_ignored(db):
    answer_id = history_module.history_repository.save_message(session_id="a", role="assistant", text="answer", intent="rag")
    record_feedback(FeedbackEvent(session_id="b", answer_id=answer_id, thumbs=-1))
    assert db[settings.mongo.fb_events_collection].count_documents({}) == 0


def test_classifier_has_separate_budget(monkeypatch):
    seen = {}
    def chat(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(invoke=lambda _: SimpleNamespace(content='{"intent":"rag"}'))
    monkeypatch.setattr(intent_route_service.llm_providers, "chat", chat)
    assert intent_route_service.call_llm_classify_intent("", "安装") == "rag"
    assert seen["timeout_seconds"] == 2.0
    assert seen["max_retries"] == 0


def test_history_same_timestamp_uses_id_as_stable_tiebreaker(db):
    collection=db[settings.mongo.chat_message_collection]
    collection.insert_one({"session_id":"tied","role":"user","text":"q","ts":1})
    answer_id=collection.insert_one({"session_id":"tied","role":"assistant","text":"a","ts":1}).inserted_id
    recent=history_module.history_repository.list_recent("tied")
    assert recent[0]["_id"] == answer_id
