import time

import mongomock
import pytest
from bson import ObjectId

from app.evolution import governance as g, repositories


@pytest.fixture
def db(monkeypatch):
    db = mongomock.MongoClient().test_governance
    monkeypatch.setattr(repositories, "get_collection", lambda name: db[name])
    monkeypatch.setattr(g, "writes_enabled", lambda: True)
    monkeypatch.setattr(g, "resolve_source", lambda ref: {
        "chunk_id": ref, "doc_id": "manual", "document_version": "v1",
        "item_name": "A", "content": "The voltage is 220 V.",
    } if ref == "chunk1" else None)
    monkeypatch.setattr(g, "index_upsert", lambda *args: True)
    monkeypatch.setattr(g, "index_deactivate", lambda *args: True)
    monkeypatch.setattr(g, "judge_support", lambda *args: None)
    return db


def candidate(db, **extra):
    doc = dict(faq_question="Voltage?", faq_answer="The voltage is 220 V.",
               source_refs=["chunk1"], item_names=["A"], status="draft", schema_version=1,
               content_version=1, publication_status="draft",
               source_versions=[dict(chunk_id="chunk1", doc_id="manual", document_version="v1")])
    doc.update(extra)
    return repositories.evolution_repo.k_candidates.insert_one(doc).inserted_id


def test_reject_forged_product_version_and_support(db):
    for patch in ({"source_refs": ["forged"]}, {"item_names": ["B"]},
                  {"source_versions": [{"chunk_id": "chunk1", "document_version": "v0"}]},
                  {"faq_answer": "The voltage is 999 V."}):
        cid = candidate(db, **patch)
        assert not g.approve_candidate(str(cid))[0]
        assert repositories.evolution_repo.k_candidates.find_one({"_id": cid})["status"] == "draft"


def test_approval_retry_idempotency_and_revoke(db, monkeypatch):
    cid = candidate(db)
    monkeypatch.setattr(g, "index_upsert", lambda *args: False)
    assert not g.approve_candidate(str(cid))[0]
    coll = repositories.evolution_repo.k_candidates
    doc = coll.find_one({"_id": cid})
    assert doc["publication_status"] == "indexing"
    assert doc["status"] == "draft"
    monkeypatch.setattr(g, "index_upsert", lambda *args: True)
    assert g.approve_candidate(str(cid))[0]
    assert g.approve_candidate(str(cid))[0]
    assert repositories.evolution_repo.publication_tasks.count_documents({}) == 1
    doc = coll.find_one({"_id": cid})
    assert g.is_retrievable(doc)
    assert g.invalidate_document("manual", "v1") == 1
    assert not g.is_retrievable(coll.find_one({"_id": cid}))


def test_leased_task_is_not_taken_by_other_worker(db):
    cid = candidate(db)
    g.queue_publication(str(cid))
    tasks = repositories.evolution_repo.publication_tasks
    tasks.update_many({}, {"$set": {"lease_until": time.time() + 60, "state": "running"}})
    assert g.run_publication_tasks()["completed"] == 0


def test_rejected_candidate_cannot_be_revived_by_publication_retry(db, monkeypatch):
    from app.evolution.approval import service
    from app.evolution import flags
    monkeypatch.setattr(flags, 'writes_enabled', lambda: True)
    cid = candidate(db)
    monkeypatch.setattr(g, 'index_upsert', lambda *args: False)
    assert not g.approve_candidate(str(cid))[0]
    assert service.reject(str(cid), 'incorrect facts')
    called = []
    monkeypatch.setattr(g, 'index_upsert', lambda *args: called.append(args) or True)
    repositories.evolution_repo.publication_tasks.update_many({}, {'$set': {'next_retry': 0}})
    g.run_publication_tasks()
    assert not called
    doc = repositories.evolution_repo.k_candidates.find_one({'_id': cid})
    assert doc['status'] == 'rejected'
    assert not g.is_retrievable(doc)


def test_reject_during_index_write_cannot_activate_candidate(db, monkeypatch):
    from app.evolution.approval import service
    from app.evolution import flags
    monkeypatch.setattr(flags, 'writes_enabled', lambda: True)
    cid = candidate(db)
    def reject_while_writing(*args):
        assert service.reject(str(cid), 'review revoked')
        return True
    deleted = []
    monkeypatch.setattr(g, 'index_upsert', reject_while_writing)
    monkeypatch.setattr(g, 'index_deactivate', lambda evo_id: deleted.append(evo_id) or True)
    assert not g.approve_candidate(str(cid))[0]
    doc = repositories.evolution_repo.k_candidates.find_one({'_id': cid})
    assert doc['status'] == 'rejected'
    assert doc['publication_status'] == 'revoked'
    assert deleted


def test_expired_publisher_cannot_delete_successor_publication(db, monkeypatch):
    cid = candidate(db)
    tasks = repositories.evolution_repo.publication_tasks
    entered = []
    deleted = []
    def upsert_with_lease_takeover(*args):
        if not entered:
            entered.append(True)
            tasks.update_many({}, {'$set': {'lease_until': 0, 'next_retry': 0}})
            assert g.run_publication_tasks(limit=1)['completed'] == 1
        return True
    monkeypatch.setattr(g, 'index_upsert', upsert_with_lease_takeover)
    monkeypatch.setattr(g, 'index_deactivate', lambda evo_id: deleted.append(evo_id) or True)
    assert g.approve_candidate(str(cid))[0]
    doc = repositories.evolution_repo.k_candidates.find_one({'_id': cid})
    assert g.is_retrievable(doc)
    assert not deleted


def test_migration_is_additive_and_defaults_to_report(db):
    cid = candidate(db, source_refs=[], schema_version=0)
    before = repositories.evolution_repo.k_candidates.find_one({"_id": cid})
    assert g.migrate_candidates()["review_required"] == 1
    assert repositories.evolution_repo.k_candidates.find_one({"_id": cid}) == before
    g.migrate_candidates(apply=True)
    after = repositories.evolution_repo.k_candidates.find_one({"_id": cid})
    assert after["publication_status"] == "review_required"
    assert after["status"] == before["status"]
    assert not g.is_retrievable(after)


def test_stale_source_is_never_retrieved(db, monkeypatch):
    cid = candidate(db)
    assert g.approve_candidate(str(cid))[0]
    doc = repositories.evolution_repo.k_candidates.find_one({"_id": cid})
    monkeypatch.setattr(g, "resolve_source", lambda ref: None)
    assert not g.is_retrievable(doc)


@pytest.mark.parametrize('sufficient, expected', [(True, 'generation_error'), (False, 'reading_miss')])
def test_server_diagnostics_reach_gap_classifier(db, monkeypatch, sufficient, expected):
    from app.evolution.feedback import collector
    from app.evolution.gap.detector import classify_gap
    monkeypatch.setattr(collector, 'writes_enabled', lambda: True)
    collector.flush_session_signals({
        'session_id': 's', 'answer_id': 'a', 'original_query': 'q', 'answer': '需核实',
        'retrieval_signals': {'zero_hit': True},
        'agentic_diagnostics': {'evidence_sufficient': sufficient, 'read_chunk_ids': ['chunk1']},
    })
    event = repositories.evolution_repo.fb_events.find_one({'answer_id': 'a'})
    assert classify_gap(event) == expected


def test_semantic_support_requires_finite_passing_score(db, monkeypatch):
    cid = candidate(db, faq_answer="This unit requires 220 volts.")
    for value in (None, 0.0, float("nan")):
        monkeypatch.setattr(g, "judge_support", lambda *args, score=value: score)
        assert not g.approve_candidate(str(cid))[0]
    monkeypatch.setattr(g, "judge_support", lambda *args: 0.99)
    assert g.approve_candidate(str(cid))[0]


def test_same_version_content_change_is_stale(db, monkeypatch):
    cid = candidate(db)
    assert g.approve_candidate(str(cid))[0]
    doc = repositories.evolution_repo.k_candidates.find_one({"_id": cid})
    monkeypatch.setattr(g, "resolve_source", lambda ref: dict(chunk_id=ref, doc_id="manual",
        document_version="v1", item_name="A", content="The voltage is 220 V. Changed."))
    assert not g.is_retrievable(doc)


def test_only_missing_knowledge_can_generate(db, monkeypatch):
    from app.evolution.candidate import generator
    monkeypatch.setattr(generator, "_call_llm", lambda *args: pytest.fail("must not generate FAQ"))
    for kind in ("retrieval_miss", "reading_miss", "generation_error"):
        assert generator.generate_candidate({"query": "Q", "gap_kind": kind}, [{"content": "facts"}]) is None


def test_generator_uses_only_read_ids_and_live_metadata(db, monkeypatch):
    from app.evolution.candidate import generator
    gap = {"query": "Voltage?", "item_names": ["A"], "gap_kind": "knowledge_missing"}
    monkeypatch.setattr(generator, "_call_llm", lambda *args: dict(
        faq_question="Voltage?", faq_answer="The voltage is 220 V.", source_refs=["forged"]))
    docs = [dict(chunk_id="chunk1", doc_id="manual", document_version="v1", content="The voltage is 220 V.")]
    rejected = generator.generate_candidate(gap, docs)
    assert rejected.status == "need_info" and not rejected.source_refs
    repositories.evolution_repo.k_candidates.delete_many({})
    monkeypatch.setattr(generator, "_call_llm", lambda *args: dict(
        faq_question="Voltage?", faq_answer="The voltage is 220 V.", source_refs=["chunk1"]))
    valid = generator.generate_candidate(gap, docs)
    assert valid.status == "draft" and valid.source_versions[0]["document_version"] == "v1"


def test_worker_recovers_expired_lease_after_restart(db, monkeypatch):
    cid = candidate(db)
    monkeypatch.setattr(g, "index_upsert", lambda *args: False)
    assert not g.approve_candidate(str(cid))[0]
    key = g.queue_publication(str(cid))
    monkeypatch.setattr(g, "index_upsert", lambda *args: True)
    repositories.evolution_repo.publication_tasks.update_one({"_id": key}, {"$set": {
        "state": "running", "lease_until": time.time() - 1, "next_retry": 0}})
    assert g.run_publication_tasks()["completed"] == 1
    assert g.is_retrievable(repositories.evolution_repo.k_candidates.find_one({"_id": cid}))
def test_revoke_filters_before_failed_index_delete_and_retries(db, monkeypatch):
    cid = candidate(db)
    assert g.approve_candidate(str(cid))[0]
    monkeypatch.setattr(g, "index_deactivate", lambda *args: False)
    assert g.revoke_candidate(str(cid))
    assert not g.is_retrievable(repositories.evolution_repo.k_candidates.find_one({"_id": cid}))
    tasks = repositories.evolution_repo.publication_tasks
    assert tasks.count_documents({"action": "revoke", "state": "retry"}) == 1
    monkeypatch.setattr(g, "index_deactivate", lambda *args: True)
    tasks.update_many({"action": "revoke"}, {"$set": {"next_retry": 0}})
    assert g.run_publication_tasks()["completed"] == 1


def test_human_adds_validated_sources_then_approves(db):
    from app.evolution.approval import service
    cid = candidate(db, status="need_info", faq_answer="", source_refs=[], source_versions=[])
    assert service.edit(str(cid), faq_answer="The voltage is 220 V.", source_refs=["chunk1"], item_names=["A"])
    assert service.approve(str(cid))[0]


def test_live_retrieval_filters_revoked_mongo_record(db):
    from app.evolution.retrieval import _format
    cid = candidate(db)
    assert g.approve_candidate(str(cid))[0]
    doc = repositories.evolution_repo.k_candidates.find_one({"_id": cid})
    hits = [{"entity": dict(evo_doc_id=doc["evo_doc_id"], faq_answer="stale or tampered vector answer")}]
    valid = _format(hits, enforce_governance=True)
    assert len(valid) == 1 and valid[0]["content"] == doc["faq_answer"]
    repositories.evolution_repo.k_candidates.update_one({"_id": cid}, {"$set": {"publication_status": "revoked"}})
    assert _format(hits, enforce_governance=True) == []


def test_grounding_uses_full_context_and_rejects_invalid_numbers(monkeypatch):
    from types import SimpleNamespace
    from app.evolution.online_eval import grounding
    captured = []
    result = {"groundedness": .9}
    class Judge:
        def invoke(self, messages):
            import json
            captured.append(messages[0].content)
            return SimpleNamespace(content=json.dumps(result))
    monkeypatch.setattr(grounding.llm_providers, "chat", lambda **kwargs: Judge())
    text = "A" * 600 + "critical source"
    assert grounding.compute_groundedness("answer", [text]) == .9
    assert text in captured[-1]
    for value in (None, True, -1, 2, float("nan")):
        result["groundedness"] = value
        assert grounding.compute_groundedness("answer", [text]) is None


def test_restart_repairs_approval_to_task_crash_window(db, monkeypatch):
    cid = candidate(db)
    original = g.queue_publication
    monkeypatch.setattr(g, "queue_publication", lambda *args: (_ for _ in ()).throw(RuntimeError("simulated process exit")))
    assert not g.approve_candidate(str(cid))[0]
    assert repositories.evolution_repo.publication_tasks.count_documents({}) == 0
    monkeypatch.setattr(g, "queue_publication", original)
    assert g.run_publication_tasks()["completed"] == 1
    assert g.is_retrievable(repositories.evolution_repo.k_candidates.find_one({"_id": cid}))


