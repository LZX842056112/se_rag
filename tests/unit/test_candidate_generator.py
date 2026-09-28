"""候选生成质量闸门测试：无证据 / 无信息结论一律登记为 need_info。"""
from __future__ import annotations

import mongomock
import pytest

from app.evolution import repositories as repositories_module
from app.evolution.candidate import generator as generator_module
from app.shared.config import settings


@pytest.fixture
def fake_mongo(monkeypatch):
    client = mongomock.MongoClient()
    db = client["test_db"]
    monkeypatch.setattr(repositories_module, "get_collection", lambda name: db[name])
    return db


def _gap(query: str = "HAK 180 的蓝牙配对码是多少？") -> dict:
    return {"session_id": "s1", "query": query, "item_names": ["Broker HAK 180"]}


def test_no_evidence_creates_need_info_instead_of_fake_faq(fake_mongo, monkeypatch):
    """没有检索证据时不得臆造 FAQ（真实事故根因）。"""
    called = {"llm": False}

    def _boom(*_args, **_kwargs):
        called["llm"] = True
        raise AssertionError("无证据时不应调用模型生成答案")

    monkeypatch.setattr(generator_module, "_call_llm", _boom)
    candidate = generator_module.generate_candidate(_gap(), context_docs=[])

    assert candidate is not None
    assert candidate.status == "need_info"
    assert candidate.faq_answer == ""
    assert called["llm"] is False
    stored = fake_mongo[settings.mongo.k_candidates_collection].find_one({"status": "need_info"})
    assert stored and stored["faq_question"] == _gap()["query"]


def test_refusal_answer_becomes_need_info(fake_mongo, monkeypatch):
    """有上下文但模型仍给出「无信息」结论时，同样不给 draft。"""
    monkeypatch.setattr(generator_module, "_call_llm", lambda *_a, **_k: {
        "faq_question": "HAK 180 的蓝牙配对码是多少？",
        "faq_answer": "参考片段中未提及该配对码，建议联系官方客服获取准确信息。",
        "source_refs": [],
    })
    candidate = generator_module.generate_candidate(
        _gap(), context_docs=[{"text": "HAK 180 支持蓝牙连接。"}]
    )
    assert candidate is not None and candidate.status == "need_info"
    assert "未包含事实" in candidate.reason


def test_informative_answer_becomes_draft(fake_mongo, monkeypatch):
    monkeypatch.setattr(generator_module, "_call_llm", lambda *_a, **_k: {
        "faq_question": "HAK 180 的额定工作温度是多少？",
        "faq_answer": "HAK 180 的额定工作温度为 0~40 摄氏度。",
        "source_refs": ["chunk-1"],
    })
    candidate = generator_module.generate_candidate(
        _gap("HAK 180 的额定工作温度是多少？"),
        context_docs=[{"text": "HAK 180 的额定工作温度为 0~40 摄氏度。"}],
    )
    assert candidate is not None
    assert candidate.status == "draft"
    assert candidate.source_refs == ["chunk-1"]
