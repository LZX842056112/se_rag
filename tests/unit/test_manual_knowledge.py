"""人工补充 FAQ 可经审批直接发布；自动候选继续要求文档来源。"""
from __future__ import annotations

from types import SimpleNamespace
import time

import mongomock
import pytest

from app.evolution import governance, repositories
from app.evolution.approval import service
from app.evolution.index import update as index
from app.evolution.retrieval import _format
from app.shared.config import settings


@pytest.fixture
def runtime(monkeypatch):
    db = mongomock.MongoClient().manual_knowledge
    monkeypatch.setattr(repositories, "get_collection", lambda name: db[name])
    monkeypatch.setattr(settings.evolution, "enabled", True)
    monkeypatch.setattr(settings.evolution, "write_enabled", True)
    calls = {"rows": [], "deletes": [], "fail": False}
    def upsert(**kwargs):
        if calls["fail"]:
            raise RuntimeError("simulated index outage")
        calls["rows"].append(kwargs)
    client = SimpleNamespace(has_collection=lambda **kwargs: True, upsert=upsert,
                             delete=lambda **kwargs: calls["deletes"].append(kwargs))
    monkeypatch.setattr(index, "milvus_gateway", SimpleNamespace(
        milvus_client=client, evolution_collection_name=settings.evolution.collection))
    monkeypatch.setattr(index, "canonical_subject", lambda names: ",".join(names))
    monkeypatch.setattr(index.llm_providers, "embed_text", lambda text: {"dense": [[1.0]], "sparse": [{1: 1.0}]})
    monkeypatch.setattr(governance, "resolve_source", lambda ref: pytest.fail("人工补充不应查询文档来源"))
    monkeypatch.setattr(governance, "judge_support", lambda *a: pytest.fail("人工审批不应要求文档评分"))
    return db, calls


def candidate(db, **changes):
    return str(db[settings.mongo.k_candidates_collection].insert_one({
        "faq_question": "测试产品支持蓝牙吗", "faq_answer": "", "status": "need_info",
        "item_names": ["测试产品"], "source_refs": [], "source_versions": [],
        "schema_version": 1, "content_version": 1, "publication_status": "draft", **changes,
    }).inserted_id)


def document(db, cid):
    from bson import ObjectId
    return db[settings.mongo.k_candidates_collection].find_one({"_id": ObjectId(cid)})


def prepare(db):
    cid = candidate(db)
    assert service.edit(cid, faq_answer="测试产品支持蓝牙连接。", source_refs=[], item_names=["测试产品"])
    return cid


def test_human_fills_answer_then_approves_directly_into_evolution_index(runtime):
    db, calls = runtime
    cid = prepare(db)
    draft = document(db, cid)
    assert draft["status"] == "draft" and draft["knowledge_origin"] == "manual"
    assert draft["manual_edited_at"] > 0 and not governance.is_retrievable(draft)
    ok, message = service.approve(cid)
    assert ok, message
    published = document(db, cid)
    assert published["status"] == published["publication_status"] == "active"
    assert published["source_refs"] == published["source_versions"] == []
    assert published["approved_content_sha256"] == governance.content_hash(published)
    assert governance.is_retrievable(published)
    assert calls["rows"][0]["collection_name"] == settings.evolution.collection
    row = calls["rows"][0]["data"][0]
    assert row["knowledge_origin"] == "manual" and row["source_refs"] == ""
    assert row["faq_answer"] == "测试产品支持蓝牙连接。"


def test_previous_edit_api_without_source_fields_still_supports_manual_fill(runtime):
    db, _ = runtime
    cid = candidate(db)
    assert service.edit(cid, faq_answer="测试产品支持蓝牙连接。")
    assert document(db, cid)["knowledge_origin"] == "manual"
    assert service.approve(cid)[0]


@pytest.mark.parametrize("changes", [{}, {"knowledge_origin": "manual"}])
def test_missing_sources_do_not_automatically_trust_unedited_candidates(runtime, changes):
    db, calls = runtime
    cid = candidate(db, status="draft", faq_answer="测试产品支持蓝牙连接。", **changes)
    assert not service.approve(cid)[0]
    assert not calls["rows"]


def test_manual_publication_failure_retries_and_becomes_retrievable_after_restart(runtime):
    db, calls = runtime
    cid = prepare(db)
    calls["fail"] = True
    assert not service.approve(cid)[0]
    assert document(db, cid)["publication_status"] == "indexing"
    assert not governance.is_retrievable(document(db, cid))
    calls["fail"] = False
    repositories.evolution_repo.publication_tasks.update_many({}, {"$set": {"next_retry": 0}})
    assert governance.run_publication_tasks()["completed"] == 1
    assert governance.is_retrievable(document(db, cid))


def test_manual_entry_is_used_by_legacy_retrieval_and_disappears_after_revoke(runtime):
    db, _ = runtime
    cid = prepare(db)
    assert service.approve(cid)[0]
    doc = document(db, cid)
    hits = [{"entity": {"evo_doc_id": doc["evo_doc_id"], "faq_answer": "stale vector text"}}]
    result = _format(hits, enforce_governance=True)
    assert len(result) == 1 and result[0]["content"] == doc["faq_answer"]
    assert service.reject(cid, "人工撤回")
    assert _format(hits, enforce_governance=True) == []


def test_editing_manual_fact_clears_approval_until_fresh_review(runtime):
    db, calls = runtime
    cid = prepare(db)
    assert service.approve(cid)[0]
    previous = document(db, cid)
    assert service.edit(cid, faq_answer="测试产品支持蓝牙连接和配对。", source_refs=[], item_names=["测试产品"])
    draft = document(db, cid)
    assert draft["approved_content_sha256"] == "" and draft["approved_at"] is None
    assert not governance.is_retrievable(draft)
    assert calls["deletes"]
    assert service.approve(cid)[0]
    current = document(db, cid)
    assert current["evo_doc_id"] != previous["evo_doc_id"]
    assert current["content_sha256"] != previous["content_sha256"]


def test_approval_does_not_publish_a_concurrent_human_edit(runtime, monkeypatch):
    db, calls = runtime
    cid = prepare(db)
    original = governance.validate_sources
    entered = []
    def validate(doc, **kwargs):
        result = original(doc, **kwargs)
        if not entered:
            entered.append(True)
            assert service.edit(cid, faq_answer="测试产品支持蓝牙连接和配对。", source_refs=[], item_names=["测试产品"])
        return result
    monkeypatch.setattr(governance, "validate_sources", validate)
    assert not service.approve(cid)[0]
    assert not calls["rows"]
    assert document(db, cid)["faq_answer"] == "测试产品支持蓝牙连接和配对。"


def test_agentic_reader_uses_approved_manual_fact_without_document_queries(runtime, monkeypatch):
    from app.rag.agentic import tools
    from app.rag.agentic.contracts import RetrievalScope
    from app.rag.agentic.evidence import docs_to_evidence
    db, _ = runtime
    cid = prepare(db)
    assert service.approve(cid)[0]
    doc = document(db, cid)
    monkeypatch.setattr(tools, "get_collection", lambda name: db[name])
    retriever = tools.RetrievalTools({"item_names": ["测试产品"]})
    monkeypatch.setattr(retriever, "_expression", lambda *args: "scoped")
    monkeypatch.setattr(retriever, "_client", lambda *a: pytest.fail("人工 FAQ 不应要求文档分块"))
    scope = RetrievalScope(item_names=["测试产品"])
    rows = retriever._approved_faq_docs([{"evo_doc_id": doc["evo_doc_id"], "knowledge_origin": "document"}],
                                       scope, time.monotonic() + 2)
    assert len(rows) == 1 and rows[0]["knowledge_origin"] == "manual"
    assert rows[0]["content"] == doc["faq_answer"] and len(docs_to_evidence(rows, scope=scope)) == 1


@pytest.mark.parametrize("answer", ["", "无法作答，请咨询客服。"])
def test_human_fill_still_requires_a_fact_before_approval(runtime, answer):
    db, calls = runtime
    cid = candidate(db)
    service.edit(cid, faq_question="测试产品支持蓝牙吗", faq_answer=answer, source_refs=[], item_names=["测试产品"])
    assert not service.approve(cid)[0]
    assert not calls["rows"]


def test_filling_invalid_document_source_does_not_silently_switch_to_manual(runtime, monkeypatch):
    db, calls = runtime
    cid = candidate(db)
    monkeypatch.setattr(governance, "resolve_source", lambda ref: None)
    assert not service.edit(cid, faq_answer="测试产品支持蓝牙连接。", source_refs=["missing"], item_names=["测试产品"])
    assert document(db, cid)["status"] == "need_info"
    assert document(db, cid).get("knowledge_origin") != "manual" and not calls["rows"]


def test_manual_entry_can_return_to_document_validation_and_invalidation(runtime, monkeypatch):
    db, _ = runtime
    cid = prepare(db)
    assert service.approve(cid)[0]
    monkeypatch.setattr(governance, "resolve_source", lambda ref: {
        "chunk_id": ref, "doc_id": "manual", "document_version": "v1", "item_name": "测试产品",
        "content": "测试产品支持蓝牙连接。",
    })
    assert service.edit(cid, source_refs=["c1"], item_names=["测试产品"])
    draft = document(db, cid)
    assert draft["knowledge_origin"] == "document" and draft["manual_edited_at"] is None
    assert service.approve(cid)[0]
    assert governance.invalidate_document("manual", "v1") == 1
    assert not governance.is_retrievable(document(db, cid))


@pytest.mark.parametrize("names", [["   "], ["\t\n"], []])
def test_manual_edit_rejects_empty_product_names(runtime, names):
    db, calls = runtime
    cid = candidate(db)
    assert not service.edit(cid, faq_answer="测试产品支持蓝牙连接。", source_refs=[], item_names=names)
    assert document(db, cid)["status"] == "need_info" and not calls["rows"]


def test_manual_edit_normalizes_product_names_before_approval(runtime):
    db, calls = runtime
    cid = candidate(db)
    assert service.edit(cid, faq_answer="测试产品支持蓝牙连接。", source_refs=[],
                        item_names=[" 测试产品 ", "", "\t", "测试产品"])
    assert document(db, cid)["item_names"] == ["测试产品"]
    assert service.approve(cid)[0]
    assert calls["rows"][0]["data"][0]["item_name"] == "测试产品"


def test_approval_rejects_legacy_manual_record_with_only_whitespace_product(runtime):
    db, calls = runtime
    cid = candidate(db, status="draft", faq_answer="测试产品支持蓝牙连接。", item_names=["   "],
                    knowledge_origin="manual", manual_edited_at=time.time())
    assert not service.approve(cid)[0]
    assert not calls["rows"]
