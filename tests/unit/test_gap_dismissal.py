"""人工忽略缺口必须作用于后续同题登记，且保留真实失败信号。"""
from __future__ import annotations

import time

import mongomock
import pytest
from bson import ObjectId

from app.evolution import issues, repositories, scheduler
from app.evolution.feedback import collector
from app.evolution.gap.detector import scan_unresolved_feedbacks
from app.evolution.gap.review import review_gap
from app.evolution.models import FeedbackEvent
from app.shared.config import settings


@pytest.fixture
def db(monkeypatch):
    database = mongomock.MongoClient().dismissal
    monkeypatch.setattr(repositories, "get_collection", lambda name: database[name])
    monkeypatch.setattr(issues, "get_collection", lambda name: database[name])
    monkeypatch.setattr(settings.evolution, "enabled", True)
    monkeypatch.setattr(settings.evolution, "write_enabled", True)
    monkeypatch.setattr(settings.evolution, "gen_context_enabled", False)
    return database


def state(**changes):
    return {"session_id": "s", "original_query": "烫金机有蓝牙吗", "item_names": ["HAK 180"],
            "intent": "rag", "answer_id": str(ObjectId()), "answer": "无法作答",
            "business_tag": "product_knowledge", **changes}


def ignore(db, **changes):
    doc = {"session_id": "s", "query": "烫金机有蓝牙吗", "item_names": ["HAK 180"],
           "business_tag": "product_knowledge", "gap_kind": "reading_miss", "status": "candidate",
           "confidence": 0.7, "ts": time.time() - 7200, **changes}
    oid = db[settings.mongo.k_gaps_collection].insert_one(doc).inserted_id
    ok, message = review_gap(str(oid), disposition="dismissed", reason="不用")
    assert ok, message
    return oid


def test_ignored_gap_prevents_new_unresolved_issue_for_same_context(db):
    ignore(db)
    assert issues.register_unresolved(state(original_query=" 烫金机有蓝牙吗？", item_names=["HAK180"])) == ""
    assert db[settings.mongo.query_issues_collection].count_documents({}) == 0


def test_negative_feedback_keeps_signal_without_recreating_ignored_issue(db, monkeypatch):
    ignore(db)
    monkeypatch.setattr(collector.history_repository, "get_answer", lambda *args: {
        "intent": "rag", "query": "烫金机有蓝牙吗", "item_names": ["HAK 180"],
        "business_tag": "product_knowledge", "citations": [],
    })
    collector.record_feedback(FeedbackEvent(session_id="s", answer_id=str(ObjectId()), thumbs=-1))
    assert db[settings.mongo.query_issues_collection].count_documents({}) == 0
    assert db[settings.mongo.query_feedback_collection].count_documents({}) == 1
    assert db[settings.mongo.fb_events_collection].count_documents({}) == 1


def test_repeat_failure_remains_in_feedback_but_does_not_create_another_gap(db):
    ignore(db)
    collector.flush_session_signals(state())
    assert db[settings.mongo.fb_events_collection].count_documents({}) == 1
    assert scan_unresolved_feedbacks() == []
    assert db[settings.mongo.k_gaps_collection].count_documents({}) == 1


def test_already_queued_gap_obeys_manual_ignore_without_generating_or_retrying(db, monkeypatch):
    original = ignore(db)
    queued = db[settings.mongo.k_gaps_collection].insert_one({
        "session_id": "s", "query": "烫金机有蓝牙吗", "item_names": ["HAK 180"],
        "gap_kind": "knowledge_missing", "status": "candidate", "ts": time.time(),
    }).inserted_id
    monkeypatch.setattr(scheduler, "generate_candidate", lambda *a: pytest.fail("已忽略同题不能生成候选"))
    result = scheduler.run_scan_and_generate_once()
    stored = db[settings.mongo.k_gaps_collection].find_one({"_id": queued})
    assert stored["status"] == "dismissed"
    assert stored["dismissed_by_gap_id"] == str(original)
    assert result["skipped"] == 1 and result["failed"] == 0
    assert db[settings.mongo.query_issues_collection].count_documents({}) == 0


def test_existing_repeat_issue_is_shown_as_ignored_without_changing_raw_history(db):
    first = issues.open_issue(state())
    assert issues.update_issue(first, owner="员工A", status="resolved", resolution="不用")[0]
    original = ignore(db)
    stale = db[settings.mongo.query_issues_collection].insert_one({
        "session_id": "s", "query": "烫金机有蓝牙吗", "item_names": ["HAK 180"],
        "status": "open", "created_at": time.time(), "owner": "", "resolution": "",
    }).inserted_id
    assert issues.list_issues(status="unresolved") == []
    ignored = issues.list_issues(status="dismissed")
    assert len(ignored) == 1 and ignored[0]["status"] == "dismissed"
    assert ignored[0]["dismissed_by_gap_id"] == str(original)
    assert ignored[0]["suppression_reason"] == "不用"
    assert len(issues.list_issues(status="resolved")) == 1
    assert db[settings.mongo.query_issues_collection].find_one({"_id": stale})["status"] == "open"
    ok, message = issues.update_issue(str(stale), owner="员工A", status="in_progress", resolution="重开")
    assert not ok and "忽略" in message


@pytest.mark.parametrize("changes", [
    {"session_id": "other"}, {"item_names": ["HL3040"]}, {"original_query": "烫金机的功率是多少"},
    {"business_tag": "after_sales"}, {"item_names": []},
])
def test_ignore_does_not_hide_failures_outside_its_context(db, changes):
    ignore(db)
    assert issues.register_unresolved(state(**changes))
    assert len(issues.list_issues(status="unresolved")) == 1


def test_completed_gap_without_ignore_still_reports_a_new_failure(db):
    oid = ignore(db)
    db[settings.mongo.k_gaps_collection].update_one({"_id": oid}, {"$set": {"status": "resolved"}})
    assert issues.register_unresolved(state())
    assert len(issues.list_issues(status="unresolved")) == 1


def test_auto_dismissed_record_without_manual_review_is_not_a_suppression_rule(db):
    db[settings.mongo.k_gaps_collection].insert_one({
        "session_id": "s", "query": "烫金机有蓝牙吗", "item_names": ["HAK 180"],
        "status": "dismissed", "ts": time.time(),
    })
    assert issues.register_unresolved(state())


def test_ignored_gap_does_not_block_scan_for_a_different_product(db):
    oid = ignore(db)
    db[settings.mongo.k_gaps_collection].update_one({"_id": oid}, {"$set": {"ts": time.time()}})
    collector.flush_session_signals(state(item_names=["HL3040"]))
    gaps = scan_unresolved_feedbacks()
    assert len(gaps) == 1 and gaps[0].item_names == ["HL3040"]


def test_business_scope_survives_feedback_and_gap_scan(db):
    ignore(db)
    collector.flush_session_signals(state(business_tag="after_sales"))
    event = db[settings.mongo.fb_events_collection].find_one()
    assert event.get("business_tag") == "after_sales"
    gaps = scan_unresolved_feedbacks()
    assert len(gaps) == 1 and gaps[0].business_tag == "after_sales"


def test_queued_other_business_is_processed_without_false_suppression_or_retry(db):
    ignore(db)
    queued = db[settings.mongo.k_gaps_collection].insert_one({
        "session_id": "s", "query": "烫金机有蓝牙吗", "item_names": ["HAK 180"],
        "business_tag": "after_sales", "gap_kind": "reading_miss", "status": "candidate", "ts": time.time(),
    }).inserted_id
    result = scheduler.run_scan_and_generate_once()
    assert result["strategy"] == 1 and result["failed"] == 0
    assert db[settings.mongo.k_gaps_collection].find_one({"_id": queued})["status"] == "strategy_pending"
    assert db[settings.mongo.query_issues_collection].find_one()["business_tag"] == "after_sales"


def test_feedback_business_scope_comes_from_saved_answer_not_client(db, monkeypatch):
    ignore(db)
    monkeypatch.setattr(collector.history_repository, "get_answer", lambda *args: {
        "intent": "rag", "query": "烫金机有蓝牙吗", "item_names": ["HAK 180"],
        "business_tag": "product_knowledge", "citations": [],
    })
    collector.record_feedback(FeedbackEvent(session_id="s", answer_id=str(ObjectId()), thumbs=-1,
                                             business_tag="after_sales"))
    assert db[settings.mongo.query_feedback_collection].find_one().get("business_tag") == "product_knowledge"
    assert db[settings.mongo.query_issues_collection].count_documents({}) == 0
