"""旧反馈更新、重启及重试不能重复派生缺口。"""
from __future__ import annotations

import time

import mongomock
import pytest
from bson import ObjectId
from pymongo.errors import DuplicateKeyError

from app.evolution import issues, repositories, scheduler
from app.evolution.feedback import collector
from app.evolution.gap import detector
from app.evolution.models import FeedbackEvent
from app.shared.config import settings


@pytest.fixture
def db(monkeypatch):
    database = mongomock.MongoClient().gap_replay
    monkeypatch.setattr(repositories, "get_collection", lambda name: database[name])
    monkeypatch.setattr(issues, "get_collection", lambda name: database[name])
    monkeypatch.setattr(settings.evolution, "enabled", True)
    monkeypatch.setattr(settings.evolution, "write_enabled", True)
    return database


def event(db, **changes):
    row = {"session_id": "s", "query": "烫金机有蓝牙吗", "item_names": ["HAK180"],
           "business_tag": "product_knowledge", "answer_id": str(ObjectId()),
           "cited_chunk_ids": ["c1"], "adopt": False, "thumbs": -1, "ts": time.time(), **changes}
    row["_id"] = db[settings.mongo.fb_events_collection].insert_one(row).inserted_id
    return row


def enforce_mock_source_index(db, monkeypatch):
    # mongomock 记录部分索引定义，但不执行跨数组的唯一多键约束。
    # 在竞态测试中补数据库约束；真实 Mongo 另做同样的交错验证。
    collection = db[settings.mongo.k_gaps_collection]
    original = collection.update_one
    def unique_update(selector, update, **kwargs):
        keys = update.get("$setOnInsert", {}).get("source_feedback_keys", [])
        added = update.get("$addToSet", {}).get("source_feedback_keys")
        if added:
            keys = [*keys, added]
        if any(collection.find_one({"_id": {"$ne": selector.get("_id")}, "source_feedback_keys": key}) for key in keys):
            raise DuplicateKeyError("unique source feedback key")
        return original(selector, update, **kwargs)
    monkeypatch.setattr(collection, "update_one", unique_update)


@pytest.mark.parametrize("state", [
    {"resolved": True, "adopt": True},
    {"resolved": True, "adopt": False},
    {"adopt": True},
])
def test_resolution_overrides_historical_thumbs_down(db, state):
    event(db, **state)
    assert detector.scan_unresolved_feedbacks() == []
    assert db[settings.mongo.k_gaps_collection].count_documents({}) == 0
    assert db[settings.mongo.fb_events_collection].find_one()["thumbs"] == -1


def test_explicit_unresolved_status_still_enters_scan(db):
    event(db, resolved=False, adopt=True)
    assert len(detector.scan_unresolved_feedbacks()) == 1


def test_updating_old_feedback_timestamp_after_window_and_restart_does_not_recreate_gap(db):
    source = event(db)
    assert len(detector.scan_unresolved_feedbacks()) == 1
    db[settings.mongo.k_gaps_collection].update_one({}, {"$set": {"status": "resolved"}})
    db[settings.mongo.fb_events_collection].update_one({"_id": source["_id"]}, {"$set": {"ts": time.time() + 7200}})
    detector.reset_scan_cursor()
    assert detector.scan_unresolved_feedbacks() == []
    assert db[settings.mongo.k_gaps_collection].count_documents({}) == 1


def test_legacy_duplicate_rows_for_one_answer_produce_only_one_gap(db):
    source = event(db)
    assert len(detector.scan_unresolved_feedbacks()) == 1
    event(db, answer_id=source["answer_id"], ts=time.time() + 7200)
    detector.reset_scan_cursor()
    assert detector.scan_unresolved_feedbacks() == []
    assert db[settings.mongo.k_gaps_collection].count_documents({}) == 1


def test_feedback_coalesced_into_existing_gap_cannot_replay_after_that_gap_is_completed(db):
    event(db)
    assert len(detector.scan_unresolved_feedbacks()) == 1
    second = event(db)
    assert detector.scan_unresolved_feedbacks() == []
    db[settings.mongo.k_gaps_collection].update_one({}, {"$set": {"status": "resolved"}})
    db[settings.mongo.fb_events_collection].update_one({"_id": second["_id"]}, {"$set": {"ts": time.time() + 7200}})
    detector.reset_scan_cursor()
    assert detector.scan_unresolved_feedbacks() == []
    assert db[settings.mongo.k_gaps_collection].count_documents({}) == 1


def test_legacy_feedback_with_completed_answer_issue_is_not_scanned_again(db):
    source = event(db)
    db[settings.mongo.query_issues_collection].insert_one({
        "session_id": "s", "answer_id": source["answer_id"], "query": source["query"],
        "item_names": ["HAK180"], "status": "resolved", "completed_at": time.time() - 7200,
    })
    assert detector.scan_unresolved_feedbacks() == []
    assert db[settings.mongo.k_gaps_collection].count_documents({}) == 0


def test_new_answer_failure_after_completed_gap_is_still_reported_inside_old_window(db):
    event(db)
    assert len(detector.scan_unresolved_feedbacks()) == 1
    db[settings.mongo.k_gaps_collection].update_one({}, {"$set": {"status": "resolved"}})
    new = event(db)
    assert len(detector.scan_unresolved_feedbacks()) == 1
    assert db[settings.mongo.k_gaps_collection].count_documents({}) == 2
    assert new["answer_id"]


def test_failed_feedback_acknowledgement_recovers_without_second_gap(db, monkeypatch):
    source = event(db)
    feedback = db[settings.mongo.fb_events_collection]
    original = feedback.update_one
    monkeypatch.setattr(feedback, "update_one", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("ack unavailable")))
    detector.scan_unresolved_feedbacks()
    assert db[settings.mongo.k_gaps_collection].count_documents({}) == 1
    monkeypatch.setattr(feedback, "update_one", original)
    feedback.update_one({"_id": source["_id"]}, {"$set": {"ts": time.time() + 7200}})
    detector.reset_scan_cursor()
    assert detector.scan_unresolved_feedbacks() == []
    assert db[settings.mongo.k_gaps_collection].count_documents({}) == 1


def test_failed_coalescing_acknowledgement_keeps_its_source_after_gap_completion(db, monkeypatch):
    event(db)
    assert len(detector.scan_unresolved_feedbacks()) == 1
    source = event(db)
    feedback = db[settings.mongo.fb_events_collection]
    original = feedback.update_one
    monkeypatch.setattr(feedback, "update_one", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("ack unavailable")))
    detector.scan_unresolved_feedbacks()
    monkeypatch.setattr(feedback, "update_one", original)
    db[settings.mongo.k_gaps_collection].update_one({}, {"$set": {"status": "resolved"}})
    feedback.update_one({"_id": source["_id"]}, {"$set": {"ts": time.time() + 7200}})
    detector.reset_scan_cursor()
    assert detector.scan_unresolved_feedbacks() == []
    assert db[settings.mongo.k_gaps_collection].count_documents({}) == 1


def test_completing_strategy_issue_allows_new_answer_failure(db):
    event(db)
    assert scheduler.run_scan_and_generate_once()["strategy"] == 1
    gap = db[settings.mongo.k_gaps_collection].find_one()
    assert issues.update_issue(gap["issue_id"], owner="员工A", status="resolved", resolution="已修复阅读策略")[0]
    assert db[settings.mongo.k_gaps_collection].find_one()["status"] == "resolved"
    event(db)
    assert len(detector.scan_unresolved_feedbacks()) == 1
    assert db[settings.mongo.k_gaps_collection].count_documents({}) == 2


def test_repeated_issue_completion_repairs_failed_gap_writeback_without_duplicate_history(db, monkeypatch):
    event(db)
    assert scheduler.run_scan_and_generate_once()["strategy"] == 1
    gaps = db[settings.mongo.k_gaps_collection]
    gap = gaps.find_one()
    original = gaps.update_one
    def unavailable(selector, update, **kwargs):
        if update.get("$set", {}).get("status") == "resolved":
            raise RuntimeError("gap writeback unavailable")
        return original(selector, update, **kwargs)
    monkeypatch.setattr(gaps, "update_one", unavailable)
    request = {"owner": "员工A", "status": "resolved", "resolution": "已修复阅读策略"}
    assert not issues.update_issue(gap["issue_id"], **request)[0]
    saved_issue = db[settings.mongo.query_issues_collection].find_one()
    assert saved_issue["status"] == "resolved"
    history_count = len(saved_issue["handling_history"])
    monkeypatch.setattr(gaps, "update_one", original)
    assert issues.update_issue(gap["issue_id"], **request)[0]
    assert gaps.find_one()["status"] == "resolved"
    assert len(db[settings.mongo.query_issues_collection].find_one()["handling_history"]) == history_count


@pytest.mark.parametrize("different", [
    {"item_names": ["HL3040"]}, {"business_tag": "after_sales"}, {"session_id": "other"},
])
def test_persistent_coalescing_keeps_known_contexts_separate(db, different):
    event(db)
    assert len(detector.scan_unresolved_feedbacks()) == 1
    event(db, **different)
    assert len(detector.scan_unresolved_feedbacks()) == 1
    assert db[settings.mongo.k_gaps_collection].count_documents({}) == 2


def test_legacy_completed_strategy_target_does_not_absorb_new_answer(db):
    event(db)
    assert scheduler.run_scan_and_generate_once()["strategy"] == 1
    gap = db[settings.mongo.k_gaps_collection].find_one()
    db[settings.mongo.query_issues_collection].update_one({"_id": ObjectId(gap["issue_id"])},
        {"$set": {"status": "resolved", "completed_at": time.time()}})
    event(db)
    assert len(detector.scan_unresolved_feedbacks()) == 1
    assert db[settings.mongo.k_gaps_collection].count_documents({}) == 2


def test_interleaved_scans_cannot_assign_same_source_to_two_gaps(db, monkeypatch):
    enforce_mock_source_index(db, monkeypatch)
    event(db)
    assert len(detector.scan_unresolved_feedbacks()) == 1
    source = event(db)
    original = detector._duplicate_gap
    interleaved = False
    def other_scanner_finishes_first(row):
        nonlocal interleaved
        if row["_id"] == source["_id"] and not interleaved:
            interleaved = True
            assert detector.scan_unresolved_feedbacks() == []
            db[settings.mongo.k_gaps_collection].update_one({}, {"$set": {"status": "resolved"}})
        return original(row)
    monkeypatch.setattr(detector, "_duplicate_gap", other_scanner_finishes_first)
    assert detector.scan_unresolved_feedbacks() == []
    assert db[settings.mongo.k_gaps_collection].count_documents({}) == 1


def test_interleaved_merge_uses_target_that_already_claimed_source(db, monkeypatch):
    enforce_mock_source_index(db, monkeypatch)
    event(db)
    first = detector.scan_unresolved_feedbacks()[0]
    collection = db[settings.mongo.k_gaps_collection]
    first_row = collection.find_one({"_id": ObjectId(first.gap_id)})
    second_id = collection.insert_one({**{key: value for key, value in first_row.items() if key != "_id"},
                                       "source_feedback_keys": []}).inserted_id
    source = event(db)
    interleaved = False
    def split_targets(row):
        nonlocal interleaved
        if row["_id"] == source["_id"] and not interleaved:
            interleaved = True
            assert detector.scan_unresolved_feedbacks() == []
            return collection.find_one({"_id": second_id})
        return first_row
    monkeypatch.setattr(detector, "_duplicate_gap", split_targets)
    assert detector.scan_unresolved_feedbacks() == []
    key = str(detector._feedback_gap_id(source))
    assert collection.count_documents({"source_feedback_keys": key}) == 1
    assert db[settings.mongo.fb_events_collection].find_one({"_id": source["_id"]})["gap_scan_id"] == first.gap_id


def test_scan_does_not_write_without_source_uniqueness_constraint(db, monkeypatch):
    event(db)
    monkeypatch.setattr(db[settings.mongo.k_gaps_collection], "create_index",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("index unavailable")))
    assert detector.scan_unresolved_feedbacks() == []
    assert db[settings.mongo.k_gaps_collection].count_documents({}) == 0


@pytest.mark.parametrize("together", [True, False])
def test_resolved_feedback_does_not_create_issue_when_thumbs_are_negative(db, monkeypatch, together):
    monkeypatch.setattr(collector.history_repository, "get_answer", lambda *a: {
        "intent": "rag", "query": "烫金机有蓝牙吗", "item_names": ["HAK180"], "citations": [],
    })
    answer_id = str(ObjectId())
    collector.record_feedback(FeedbackEvent(session_id="s", answer_id=answer_id, resolved=True,
                                            thumbs=-1 if together else None))
    if not together:
        collector.record_feedback(FeedbackEvent(session_id="s", answer_id=answer_id, thumbs=-1))
    assert db[settings.mongo.query_issues_collection].count_documents({}) == 0
    saved = db[settings.mongo.query_feedback_collection].find_one()
    assert saved["resolved"] is True and saved["thumbs"] == -1


def test_late_automatic_signal_preserves_explicit_resolution(db, monkeypatch):
    monkeypatch.setattr(collector.history_repository, "get_answer", lambda *a: {
        "intent": "rag", "query": "烫金机有蓝牙吗", "item_names": ["HAK180"], "citations": [],
    })
    answer_id = str(ObjectId())
    collector.record_feedback(FeedbackEvent(session_id="s", answer_id=answer_id, thumbs=-1, resolved=True))
    collector.flush_session_signals({"session_id": "s", "answer_id": answer_id, "intent": "rag",
        "original_query": "烫金机有蓝牙吗", "item_names": ["HAK180"], "answer": "无法作答"})
    saved = db[settings.mongo.fb_events_collection].find_one()
    assert saved["resolved"] is True and saved["adopt"] is True
    assert detector.scan_unresolved_feedbacks() == []
