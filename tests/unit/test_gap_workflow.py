"""强缺口必须进入可见的处理流程，不能在扫描后无声跳过。"""
from __future__ import annotations

import time

import mongomock
import pytest

from app.evolution import issues, repositories, scheduler
from app.evolution.gap.detector import scan_unresolved_feedbacks
from app.shared.config import settings


@pytest.fixture
def db(monkeypatch):
    database = mongomock.MongoClient().gap_workflow
    monkeypatch.setattr(repositories, "get_collection", lambda name: database[name])
    monkeypatch.setattr(issues, "get_collection", lambda name: database[name])
    monkeypatch.setattr(settings.evolution, "enabled", True)
    monkeypatch.setattr(settings.evolution, "write_enabled", True)
    return database


def gap(db, **values):
    doc = {"session_id": "s", "query": "HAK180的功率是多少？", "item_names": ["HAK180"],
           "gap_kind": "reading_miss", "confidence": 0.7, "status": "candidate", "ts": time.time()}
    doc.update(values)
    return db[settings.mongo.k_gaps_collection].insert_one(doc).inserted_id


def test_legacy_greeting_feedback_never_becomes_knowledge_gap(db):
    db[settings.mongo.fb_events_collection].insert_one({
        "session_id": "s", "query": "你好", "adopt": False, "thumbs": -1, "ts": time.time(),
    })
    assert scan_unresolved_feedbacks() == []
    assert db[settings.mongo.k_gaps_collection].count_documents({}) == 0


@pytest.mark.parametrize("kind", ["retrieval_miss", "reading_miss", "generation_error"])
def test_previously_skipped_gap_becomes_strategy_issue(db, monkeypatch, kind):
    oid = gap(db, gap_kind=kind)
    monkeypatch.setattr(scheduler, "generate_candidate", lambda *a: pytest.fail("策略缺口不能生成事实"))
    result = scheduler.run_scan_and_generate_once()
    stored = db[settings.mongo.k_gaps_collection].find_one({"_id": oid})
    assert stored["status"] == "strategy_pending"
    assert stored["processing_reason"]
    issue = db[settings.mongo.query_issues_collection].find_one()
    assert issue["gap_id"] == str(oid) and issue["gap_kind"] == kind
    assert result["strategy"] == 1 and result["failed"] == 0
    scheduler.run_scan_and_generate_once()
    assert db[settings.mongo.query_issues_collection].count_documents({}) == 1


def test_old_greeting_gap_is_preserved_but_dismissed(db):
    oid = gap(db, query="你好", gap_kind="retrieval_miss")
    scheduler.run_scan_and_generate_once()
    stored = db[settings.mongo.k_gaps_collection].find_one({"_id": oid})
    assert stored["status"] == "dismissed"
    assert stored["processing_reason"]
    assert db[settings.mongo.k_candidates_collection].count_documents({}) == 0


def test_admin_can_confirm_missing_knowledge_without_inventing_answer(db):
    from app.evolution.gap.review import review_gap
    oid = gap(db, status="strategy_pending")
    ok, message = review_gap(str(oid), disposition="knowledge_missing", reason="已核对手册，需要补充功率资料")
    assert ok, message
    candidate = db[settings.mongo.k_candidates_collection].find_one({"gap_id": str(oid)})
    assert candidate["status"] == "need_info"
    assert candidate["gap_kind"] == "knowledge_missing"
    assert candidate["faq_answer"] == "" and candidate["source_refs"] == []
    stored = db[settings.mongo.k_gaps_collection].find_one({"_id": oid})
    assert stored["review_reason"] == "已核对手册，需要补充功率资料"
    assert stored["candidate_id"] == str(candidate["_id"])
    assert review_gap(str(oid), disposition="knowledge_missing", reason="重复确认")[0]
    assert db[settings.mongo.k_candidates_collection].count_documents({}) == 1


def test_gap_list_exposes_unprocessed_and_strategy_gaps(db):
    from app.evolution.gap.review import list_gaps
    first = gap(db)
    second = gap(db, query="另一个问题", status="strategy_pending", processing_reason="等待阅读策略修复")
    rows = list_gaps()
    assert {row["id"] for row in rows} == {str(first), str(second)}
    assert next(row for row in rows if row["id"] == str(second))["processing_reason"] == "等待阅读策略修复"


def test_gap_review_refuses_greeting_and_disabled_writes(db, monkeypatch):
    from app.evolution.gap.review import review_gap
    oid = gap(db, query="你好")
    assert not review_gap(str(oid), disposition="knowledge_missing", reason="这只是问候")[0]
    monkeypatch.setattr(settings.evolution, "write_enabled", False)
    oid = gap(db)
    assert not review_gap(str(oid), disposition="knowledge_missing", reason="需要补充")[0]
    assert db[settings.mongo.k_candidates_collection].count_documents({}) == 0


def test_interrupted_manual_review_resumes_without_duplicate_candidate(db, monkeypatch):
    from app.evolution.gap.review import review_gap
    oid = gap(db)
    collection = db[settings.mongo.k_candidates_collection]
    original = collection.update_one
    def unavailable(*args, **kwargs):
        raise RuntimeError("temporary storage failure")
    monkeypatch.setattr(collection, "update_one", unavailable)
    assert not review_gap(str(oid), disposition="knowledge_missing", reason="已核对手册")[0]
    assert db[settings.mongo.k_gaps_collection].find_one({"_id": oid})["status"] == "reviewing"
    monkeypatch.setattr(collection, "update_one", original)
    scheduler.run_scan_and_generate_once()
    scheduler.run_scan_and_generate_once()
    assert collection.count_documents({}) == 1
    assert db[settings.mongo.k_gaps_collection].find_one({"_id": oid})["status"] == "need_info"


def test_dismissed_gap_does_not_create_candidate(db):
    from app.evolution.gap.review import review_gap
    oid = gap(db)
    assert review_gap(str(oid), disposition="dismissed", reason="非产品知识问题")[0]
    scheduler.run_scan_and_generate_once()
    assert db[settings.mongo.k_gaps_collection].find_one({"_id": oid})["status"] == "dismissed"
    assert db[settings.mongo.k_candidates_collection].count_documents({}) == 0


def test_open_gap_is_visible_when_more_than_100_newer_gaps_are_resolved(db):
    from app.evolution.gap.review import list_gaps
    oid = gap(db, ts=1)
    for index in range(101):
        gap(db, ts=100 + index, status="resolved")
    assert str(oid) in {row["id"] for row in list_gaps()}


def test_failed_old_gap_does_not_starve_new_gap(db, monkeypatch):
    monkeypatch.setattr(settings.evolution, "scan_batch", 1)
    monkeypatch.setattr(settings.evolution, "gen_context_enabled", False)
    monkeypatch.setattr(scheduler, "generate_candidate", lambda *a: None)
    old = gap(db, ts=1, gap_kind="knowledge_missing")
    new = gap(db, ts=2, gap_kind="reading_miss")
    scheduler.run_scan_and_generate_once()
    # 模拟下个调度周期：最老失败项已经可以重试。
    db[settings.mongo.k_gaps_collection].update_one({"_id": old}, {"$set": {"next_retry_at": 0}})
    scheduler.run_scan_and_generate_once()
    assert db[settings.mongo.k_gaps_collection].find_one({"_id": new})["status"] == "strategy_pending"


@pytest.mark.parametrize("kind", ["knowledge_missing", "reading_miss"])
def test_manual_ignore_cannot_race_background_side_effect(db, monkeypatch, kind):
    from app.evolution.gap.review import review_gap
    from app.evolution.candidate.generator import generate_candidate
    oid = gap(db, gap_kind=kind)
    monkeypatch.setattr(settings.evolution, "gen_context_enabled", False)
    if kind == "knowledge_missing":
        def generate(*args):
            assert not review_gap(str(oid), disposition="dismissed", reason="并发忽略")[0]
            return generate_candidate(*args)
        monkeypatch.setattr(scheduler, "generate_candidate", generate)
    else:
        original = issues.open_issue
        def open_issue(state):
            assert not review_gap(str(oid), disposition="dismissed", reason="并发忽略")[0]
            return original(state)
        monkeypatch.setattr(issues, "open_issue", open_issue)
    result = scheduler.run_scan_and_generate_once()
    assert result["failed"] == 0
    assert db[settings.mongo.k_gaps_collection].find_one({"_id": oid})["status"] != "dismissed"


def test_manual_review_reuses_candidate_written_before_gap_update(db):
    from app.evolution.gap.review import review_gap
    oid = gap(db)
    existing = db[settings.mongo.k_candidates_collection].insert_one({
        "gap_id": str(oid), "faq_question": "HAK180的功率是多少？", "faq_answer": "",
        "source_refs": [], "status": "need_info",
    }).inserted_id
    assert review_gap(str(oid), disposition="knowledge_missing", reason="中断后重试")[0]
    assert db[settings.mongo.k_candidates_collection].count_documents({"gap_id": str(oid)}) == 1
    assert db[settings.mongo.k_gaps_collection].find_one({"_id": oid})["candidate_id"] == str(existing)


def test_expired_worker_lease_links_existing_candidate_without_regeneration(db, monkeypatch):
    oid = gap(db, gap_kind="knowledge_missing", status="processing", resume_status="candidate",
              processing_owner="dead-process", lease_until=time.time() - 10)
    candidate_id = db[settings.mongo.k_candidates_collection].insert_one({
        "gap_id": str(oid), "status": "need_info", "faq_question": "q", "faq_answer": "",
    }).inserted_id
    monkeypatch.setattr(scheduler, "generate_candidate", lambda *a: pytest.fail("恢复不能重复调用模型"))
    scheduler.run_scan_and_generate_once()
    stored = db[settings.mongo.k_gaps_collection].find_one({"_id": oid})
    assert stored["status"] == "need_info" and stored["candidate_id"] == str(candidate_id)
    assert "processing_owner" not in stored


def test_live_worker_lease_is_not_processed_again(db):
    oid = gap(db, status="processing", resume_status="candidate", processing_owner="live",
              lease_until=time.time() + 300)
    assert scheduler.run_scan_and_generate_once()["scanned"] == 0
    assert db[settings.mongo.k_gaps_collection].find_one({"_id": oid})["processing_owner"] == "live"


def test_concurrent_manual_reviews_create_one_candidate(db):
    from concurrent.futures import ThreadPoolExecutor
    from app.evolution.gap.review import review_gap
    oid = gap(db)
    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(lambda _: review_gap(str(oid), disposition="knowledge_missing", reason="已核对手册"), range(8)))
    assert all(ok for ok, _ in outcomes)
    assert db[settings.mongo.k_candidates_collection].count_documents({"gap_id": str(oid)}) == 1


def _expire_claim_and_dismiss(db, oid):
    """旧任务暂停，新任务接管后失败，管理员再成功忽略。"""
    from app.evolution.gap.review import review_gap
    gaps = db[settings.mongo.k_gaps_collection]
    gaps.update_one({"_id": oid}, {"$set": {"lease_until": time.time() - 1}})
    replacement = scheduler._claim_gap(gaps.find_one({"_id": oid}))
    assert replacement
    scheduler._finish_gap(replacement, {"status": "candidate"})
    assert review_gap(str(oid), disposition="dismissed", reason="已确认无需处理")[0]


@pytest.mark.parametrize("kind", ["knowledge_missing", "reading_miss"])
def test_expired_worker_cannot_register_after_successful_ignore(db, monkeypatch, kind):
    from app.evolution.candidate.generator import generate_candidate
    oid = gap(db, gap_kind=kind)
    monkeypatch.setattr(settings.evolution, "gen_context_enabled", False)
    if kind == "knowledge_missing":
        def generate(*args):
            _expire_claim_and_dismiss(db, oid)
            return generate_candidate(*args)
        monkeypatch.setattr(scheduler, "generate_candidate", generate)
    else:
        original = issues.open_issue
        def open_issue(state):
            _expire_claim_and_dismiss(db, oid)
            return original(state)
        monkeypatch.setattr(issues, "open_issue", open_issue)
    result = scheduler.run_scan_and_generate_once()
    assert db[settings.mongo.k_gaps_collection].find_one({"_id": oid})["status"] == "dismissed"
    assert db[settings.mongo.k_candidates_collection].count_documents({}) == 0
    assert db[settings.mongo.query_issues_collection].count_documents({}) == 0
    assert result["generated"] == 0 and result["strategy"] == 0


@pytest.mark.parametrize("kind", ["knowledge_missing", "reading_miss"])
def test_lease_lost_during_insert_keeps_unlinked_record_hidden(db, monkeypatch, kind):
    from app.evolution.approval.service import list_candidates
    oid = gap(db, gap_kind=kind)
    monkeypatch.setattr(settings.evolution, "gen_context_enabled", False)
    name = settings.mongo.k_candidates_collection if kind == "knowledge_missing" else settings.mongo.query_issues_collection
    collection = db[name]
    original = collection.update_one
    def paused_insert(match, update, **kwargs):
        if kwargs.get("upsert"):
            _expire_claim_and_dismiss(db, oid)
        return original(match, update, **kwargs)
    monkeypatch.setattr(collection, "update_one", paused_insert)
    result = scheduler.run_scan_and_generate_once()
    assert db[settings.mongo.k_gaps_collection].find_one({"_id": oid})["status"] == "dismissed"
    assert list_candidates() == [] and issues.list_issues() == []
    assert result["generated"] == 0 and result["strategy"] == 0
    assert collection.find_one()["registration_status"] == "staged"


@pytest.mark.parametrize("kind", ["knowledge_missing", "reading_miss"])
def test_restart_recovers_registration_after_gap_link(db, monkeypatch, kind):
    from app.evolution.approval.service import list_candidates
    oid = gap(db, gap_kind=kind)
    monkeypatch.setattr(settings.evolution, "gen_context_enabled", False)
    name = settings.mongo.k_candidates_collection if kind == "knowledge_missing" else settings.mongo.query_issues_collection
    collection = db[name]
    original = collection.update_one
    def fail_activation(match, update, **kwargs):
        if update.get("$set", {}).get("registration_status") == "ready":
            raise RuntimeError("crash after gap link")
        return original(match, update, **kwargs)
    monkeypatch.setattr(collection, "update_one", fail_activation)
    scheduler.run_scan_and_generate_once()
    assert collection.find_one()["registration_status"] == "staged"
    assert list_candidates() == [] and issues.list_issues() == []
    monkeypatch.setattr(collection, "update_one", original)
    scheduler.run_scan_and_generate_once()
    assert collection.count_documents({}) == 1
    assert collection.find_one()["registration_status"] == "ready"
    assert len(list_candidates()) + len(issues.list_issues()) == 1
    stored = db[settings.mongo.k_gaps_collection].find_one({"_id": oid})
    assert stored["status"] in {"need_info", "strategy_pending"}


def test_manual_confirmation_still_succeeds_if_other_request_finishes_first(db, monkeypatch):
    from app.evolution.gap import review
    oid = gap(db)
    original = review.store_candidate
    nested = []
    def paused_store(candidate):
        if not nested:
            nested.append(True)
            assert review.review_gap(str(oid), disposition="knowledge_missing", reason="第二个请求完成")[0]
        return original(candidate)
    monkeypatch.setattr(review, "store_candidate", paused_store)
    ok, message = review.review_gap(str(oid), disposition="knowledge_missing", reason="第一个请求恢复")
    assert ok, message
    assert db[settings.mongo.k_candidates_collection].count_documents({}) == 1


def test_orphan_staged_candidate_does_not_block_a_new_gap_for_same_question(db, monkeypatch):
    from app.evolution.approval.service import list_candidates
    old = gap(db, status="dismissed")
    db[settings.mongo.k_candidates_collection].insert_one({
        "gap_id": str(old), "faq_question": "HAK180的功率是多少？", "faq_answer": "",
        "status": "need_info", "registration_status": "staged",
    })
    new = gap(db, gap_kind="knowledge_missing")
    monkeypatch.setattr(settings.evolution, "gen_context_enabled", False)
    result = scheduler.run_scan_and_generate_once()
    assert result["generated"] == 1 and result["failed"] == 0
    candidates = list_candidates()
    assert len(candidates) == 1 and candidates[0]["gap_id"] == str(new)
    assert db[settings.mongo.k_gaps_collection].find_one({"_id": old})["status"] == "dismissed"
