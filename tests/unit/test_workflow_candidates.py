import time

import mongomock
import pytest

from app.evolution import repositories
from app.evolution.feedback.collector import _emit
from app.evolution.models import FeedbackEvent, KnowledgeCandidate
from app.shared.config import settings


@pytest.fixture
def db(monkeypatch):
    database = mongomock.MongoClient().test
    monkeypatch.setattr(repositories, "get_collection", lambda name: database[name])
    return database


def test_feedback_identity_survives_gap_merge_approval_and_publication(db, monkeypatch):
    from app.evolution import governance
    from app.evolution.approval.service import approve, edit
    from app.evolution.gap import detector
    from app.evolution.workflow_candidates import list_candidate_runs

    monkeypatch.setattr(settings.evolution, "write_enabled", True)
    repo = repositories.evolution_repo
    for answer_id in ("one", "two"):
        _emit(FeedbackEvent(session_id="test", answer_id=answer_id, query="question", resolved=False,
                            gap_kind="knowledge_missing"))
    ids = {str(doc["_id"]) for doc in repo.fb_events.find()}
    rows = list_candidate_runs()
    assert {row["id"] for row in rows} == ids
    assert all(row["nodes"]["gap"]["status"] == "waiting" for row in rows)

    detector.scan_unresolved_feedbacks()
    gap = repo.k_gaps.find_one()
    assert len(gap["source_feedback_keys"]) == 2
    draft = KnowledgeCandidate(gap_id=str(gap["_id"]), faq_question="测试设备支持蓝牙吗", faq_answer="",
                               item_names=["测试设备"], status="need_info")
    candidate_id = repo.k_candidates.insert_one(draft.model_dump()).inserted_id
    repo.k_gaps.update_one({"_id": gap["_id"]}, {"$set": {"candidate_id": str(candidate_id), "status": "need_info"}})
    rows = list_candidate_runs()
    assert {row["id"] for row in rows} == ids
    assert all(row["nodes"]["supplement"]["status"] == "waiting" for row in rows)

    assert edit(str(candidate_id), faq_answer="测试设备支持蓝牙连接。")
    assert all(row["nodes"]["approval"]["status"] == "waiting" for row in list_candidate_runs())
    assert all(row["nodes"]["supplement"]["status"] == "completed" for row in list_candidate_runs())
    repo.publication_tasks.insert_one({
        "_id": governance.publication_key(str(candidate_id), 1, "old-digest"), "candidate_id": candidate_id,
        "content_version": 1, "digest": "old-digest", "state": "running", "lease_until": time.time() + 900,
    })
    monkeypatch.setattr(governance, "index_upsert", lambda *_: False)
    assert not approve(str(candidate_id))[0]
    assert all(row["nodes"]["publish"]["status"] == "retrying" and row["attempt"] == 1 for row in list_candidate_runs())

    def publish(*_):
        assert all(row["nodes"]["publish"]["status"] == "running" and row["attempt"] == 2 for row in list_candidate_runs())
        return True

    monkeypatch.setattr(governance, "index_upsert", publish)
    repo.publication_tasks.update_one({"state": "retry"}, {"$set": {"next_retry": 0}})
    assert governance.run_publication_tasks()["completed"] == 1
    rows = list_candidate_runs()
    assert {row["id"] for row in rows} == ids
    assert all(row["status"] == "completed" and row["attempt"] == 2 for row in rows)
    assert all(row["nodes"]["supplement"]["status"] == "completed" for row in rows)


def test_closed_feedback_weak_signal_and_unlinked_candidate_have_distinct_states(db, monkeypatch):
    from app.evolution import workflow_candidates
    from app.evolution.gap import detector

    monkeypatch.setattr(settings.evolution, "write_enabled", True)
    monkeypatch.setattr(settings.evolution, "gap_strong_threshold", 1.1)
    repo = repositories.evolution_repo
    now = time.time()
    repo.fb_events.insert_many([
        {"_id": "open", "query": "quality problem", "ts": now, "thumbs": -1},
        {"_id": "closed", "query": "another problem", "ts": now, "resolved": True,
         "workflow_entered_at": now - 1},
    ])
    legacy = repo.k_candidates.insert_one({"status": "draft", "ts": now}).inserted_id
    detector.scan_unresolved_feedbacks()
    rows = {row["id"]: row for row in workflow_candidates.list_candidate_runs()}
    assert rows["open"]["nodes"]["gap"]["status"] == "completed"
    assert rows["open"]["status"] == "closed"
    assert rows["closed"]["status"] == "closed"
    assert rows[f"candidate:{legacy}"]["nodes"]["feedback"]["status"] == "unknown"


def test_recent_rejection_sorts_by_decision_time(db, monkeypatch):
    from app.evolution.approval.service import reject
    from app.evolution.workflow_candidates import list_candidate_runs

    monkeypatch.setattr(settings.evolution, "write_enabled", True)
    candidates = repositories.evolution_repo.k_candidates
    now = time.time()
    old = candidates.insert_one({"status": "draft", "ts": now - 100}).inserted_id
    candidates.insert_many({"status": "rejected", "ts": now - index} for index in range(1, 51))
    assert reject(str(old))
    assert list_candidate_runs()[0]["id"] == f"candidate:{old}"
