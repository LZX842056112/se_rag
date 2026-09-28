"""自进化信号完整性回归测试（联调实测缺陷）。

D13：Milvus 数值型主键会被解析成 int 落进 ``state["cited_chunk_ids"]``，
     而 ``FeedbackEvent.cited_chunk_ids`` 要求 ``list[str]`` → Pydantic 校验失败，
     ``flush_session_signals`` 的异常被 catch 成 warning，导致「检索到却答不出」的
     自动会话信号被静默丢弃，缺口漏检。
D14：显式反馈（前端 👎）此前不携带 ``item_names``，缺口/候选主体丢失。
"""
from __future__ import annotations

import mongomock
import pytest

from app.evolution import repositories as repositories_module
from app.evolution.feedback.collector import flush_session_signals, record_feedback
from app.evolution.models import FeedbackEvent
from app.rag.query.answer_service import backfill_evolution_outputs
from app.shared.config import settings

NO_ANSWER = "现有参考内容与历史对话中未查询到该问题相关信息，无法作答"


@pytest.fixture
def fake_mongo(monkeypatch):
    """用 mongomock 替换集合获取函数，避免真实连库。"""
    client = mongomock.MongoClient()
    db = client["test_db"]
    monkeypatch.setattr(repositories_module, "get_collection", lambda name: db[name])
    return db


def _fb_docs(fake_mongo) -> list[dict]:
    return list(fake_mongo[settings.mongo.fb_events_collection].find({}))


def test_backfill_normalizes_numeric_cited_ids(monkeypatch):
    """数值主键必须被归一化为 str，且自进化 id 单独归集。"""
    monkeypatch.setattr(settings.evolution, "enabled", False)
    state = {
        "reranked_docs": [
            {"chunk_id": 469389094813929754, "type": "chunk", "source": "kb", "text": "a"},
            {"chunk_id": "evo_d805489677bc", "type": "chunk", "source": "evolution", "text": "b"},
        ],
    }
    backfill_evolution_outputs(state)
    assert state["cited_chunk_ids"] == ["469389094813929754", "evo_d805489677bc"]
    assert state["faq_evo_ids"] == ["evo_d805489677bc"]
    assert all(isinstance(cid, str) for cid in state["cited_chunk_ids"])


def test_backfilled_state_is_accepted_by_feedback_event(monkeypatch):
    """D13 回归：backfill 后的状态可直接构造 FeedbackEvent，不再触发校验失败。"""
    monkeypatch.setattr(settings.evolution, "enabled", False)
    state = {
        "session_id": "s-d13",
        "original_query": "某机型防水等级？",
        "answer": NO_ANSWER,
        "item_names": ["某机型"],
        "reranked_docs": [
            {"chunk_id": 469389094813929754, "type": "chunk", "source": "kb", "text": "a"},
        ],
    }
    backfill_evolution_outputs(state)
    FeedbackEvent(
        session_id=state["session_id"],
        query=state["original_query"],
        cited_chunk_ids=state["cited_chunk_ids"],
        item_names=state["item_names"],
    )


def test_flush_session_signals_persists_numeric_cited_ids_and_item_names(fake_mongo, monkeypatch):
    """D13 端到端：带数值主键的「答不出」信号应成功落库并保留主体。"""
    monkeypatch.setattr(settings.evolution, "enabled", True)
    flush_session_signals({
        "session_id": "s-flush",
        "original_query": "某机型防水等级？",
        "answer": NO_ANSWER,
        "cited_chunk_ids": ["469389094813929754"],
        "item_names": ["某机型"],
        "retrieval_signals": {"zero_hit": False},
    })
    docs = _fb_docs(fake_mongo)
    assert len(docs) == 1
    assert docs[0]["cited_chunk_ids"] == ["469389094813929754"]
    assert docs[0]["item_names"] == ["某机型"]
    assert docs[0]["thumbs"] == -1 and docs[0]["adopt"] is False


def test_flush_session_signals_skips_answered_queries(fake_mongo, monkeypatch):
    """正常作答（无缺口信号）不应写入未解决反馈。"""
    monkeypatch.setattr(settings.evolution, "enabled", True)
    flush_session_signals({
        "session_id": "s-ok",
        "original_query": "q",
        "answer": "该机型额定功率为 88W。",
        "retrieval_signals": {"zero_hit": False, "no_retrieval": False},
    })
    assert _fb_docs(fake_mongo) == []


def test_record_feedback_persists_item_names(fake_mongo, monkeypatch):
    """D14 回归：显式反馈携带的 item_names 必须落库，供缺口/候选透传。"""
    monkeypatch.setattr(settings.evolution, "enabled", True)
    record_feedback(FeedbackEvent(
        session_id="s-d14", query="某机型保修期？", thumbs=-1, item_names=["某机型"],
    ))
    docs = _fb_docs(fake_mongo)
    assert len(docs) == 1
    assert docs[0]["item_names"] == ["某机型"]


def test_query_response_schema_exposes_item_names():
    """D14 契约：非流式查询响应必须把 item_names 暴露给前端。"""
    from app.api.schema.query_schema import QueryNotStreamResponseSchema

    resp = QueryNotStreamResponseSchema(
        message="m", session_id="s", answer="a", done_list=[], image_urls=[],
    )
    assert resp.item_names == []


def test_chat_frontend_echoes_item_names_on_feedback():
    """D14 前端回归：点踩请求体必须带上该回答的 item_names。"""
    from pathlib import Path

    source = (Path(__file__).resolve().parents[2] / "app" / "resources" / "js" / "chat.js") \
        .read_text(encoding="utf-8")
    assert "item_names: meta.itemNames" in source
    assert "item.item_names" in source
