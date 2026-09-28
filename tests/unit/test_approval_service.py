"""审批服务测试：无事实的候选不能被通过；编辑补齐后自动回到 draft。"""
from __future__ import annotations

import mongomock
import pytest

from app.evolution import repositories as repositories_module
from app.evolution.approval import service as approval_service
from app.evolution.models import KnowledgeCandidate
from app.shared.config import settings


@pytest.fixture
def fake_mongo(monkeypatch):
    client = mongomock.MongoClient()
    db = client["test_db"]
    monkeypatch.setattr(repositories_module, "get_collection", lambda name: db[name])
    return db


@pytest.fixture
def upsert_ok(monkeypatch):
    """避免真实写 Milvus；记录是否被调用。"""
    called = {"count": 0}

    def _upsert(evo_doc_id: str, item: KnowledgeCandidate) -> bool:
        called["count"] += 1
        return True

    monkeypatch.setattr(approval_service, "upsert_item", _upsert)
    return called


def _insert(db, **overrides) -> str:
    payload = {
        "faq_question": "HAK 180 的蓝牙配对码是多少？",
        "faq_answer": "配对码默认为 0000。",
        "status": "draft",
    }
    payload.update(overrides)
    result = db[settings.mongo.k_candidates_collection].insert_one(payload)
    return str(result.inserted_id)


def test_need_info_cannot_be_approved(fake_mongo, upsert_ok):
    cid = _insert(fake_mongo, status="need_info", faq_answer="")
    ok, message = approval_service.approve(cid)
    assert ok is False
    assert "编辑" in message
    assert upsert_ok["count"] == 0


def test_refusal_answer_cannot_be_approved(fake_mongo, upsert_ok):
    cid = _insert(fake_mongo, faq_answer="未提及该信息，建议联系官方客服获取。")
    ok, message = approval_service.approve(cid)
    assert ok is False
    assert "未包含可入库的事实" in message
    assert upsert_ok["count"] == 0


def test_good_draft_can_be_approved(fake_mongo, upsert_ok):
    cid = _insert(fake_mongo)
    ok, message = approval_service.approve(cid)
    assert (ok, message) == (True, "")
    stored = fake_mongo[settings.mongo.k_candidates_collection].find_one({"_id": __import__("bson").ObjectId(cid)})
    assert stored["status"] == "active" and stored["evo_doc_id"].startswith("evo_")
    assert upsert_ok["count"] == 1


def test_edit_fills_answer_and_returns_to_draft(fake_mongo, upsert_ok):
    cid = _insert(fake_mongo, status="need_info", faq_answer="")
    assert approval_service.edit(cid, faq_answer="配对码默认为 0000，可在设置中修改。") is True
    stored = fake_mongo[settings.mongo.k_candidates_collection].find_one({"_id": __import__("bson").ObjectId(cid)})
    assert stored["status"] == "draft"
    ok, message = approval_service.approve(cid)
    assert (ok, message) == (True, "")


def test_edit_with_refusal_text_keeps_need_info(fake_mongo):
    cid = _insert(fake_mongo, status="need_info", faq_answer="")
    assert approval_service.edit(cid, faq_answer="文档中未说明，建议咨询厂商。") is True
    stored = fake_mongo[settings.mongo.k_candidates_collection].find_one({"_id": __import__("bson").ObjectId(cid)})
    assert stored["status"] == "need_info"


def test_approve_missing_candidate_returns_message(fake_mongo, upsert_ok):
    ok, message = approval_service.approve("6aba50cb5bc4916bb36526bf")
    assert ok is False and message == "候选不存在"


def test_remove_candidate_deactivates_and_deletes(fake_mongo, monkeypatch):
    """下架：已入库条目要同时移出向量库（误批的伪知识靠它清理）。"""
    deactivated: list[str] = []
    monkeypatch.setattr(approval_service, "deactivate", lambda evo_id: deactivated.append(evo_id) or True)
    cid = _insert(fake_mongo, status="active", evo_doc_id="evo_test123")
    assert approval_service.remove_candidate(cid) is True
    assert deactivated == ["evo_test123"]
    assert fake_mongo[settings.mongo.k_candidates_collection].count_documents({}) == 0
