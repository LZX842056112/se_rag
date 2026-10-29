"""治理清理保留补偿写入，撤回只执行一次状态转换。"""
import time

import mongomock


def test_source_gap_updates_are_best_effort(monkeypatch):
    from app.evolution import repositories
    from app.evolution.gap.registration import mark_source_gap
    db = mongomock.MongoClient().db
    monkeypatch.setattr(repositories, "get_collection", lambda name: db[name])
    from app.shared.config import settings
    oid = db[settings.mongo.k_gaps_collection].insert_one({"status": "candidate"}).inserted_id
    mark_source_gap(str(oid), "resolved")
    assert db[settings.mongo.k_gaps_collection].find_one({"_id": oid})["status"] == "resolved"
    mark_source_gap("invalid", "rejected")
    monkeypatch.setattr(repositories, "get_collection", lambda name: (_ for _ in ()).throw(RuntimeError("offline")))
    mark_source_gap(str(oid), "rejected")


def test_repeat_approval_keeps_source_gap_compensation(monkeypatch):
    from app.evolution import governance, repositories
    from app.evolution.approval import service
    from app.shared.config import settings
    db = mongomock.MongoClient().db
    monkeypatch.setattr(repositories, "get_collection", lambda name: db[name])
    gap_id = db[settings.mongo.k_gaps_collection].insert_one({"status": "candidate"}).inserted_id
    candidate_id = db[settings.mongo.k_candidates_collection].insert_one({"status": "active", "gap_id": str(gap_id)}).inserted_id
    monkeypatch.setattr(governance, "approve_candidate", lambda *a: (True, "已发布"))
    assert service.approve(str(candidate_id))[0]
    assert service.approve(str(candidate_id))[0]
    assert db[settings.mongo.k_gaps_collection].find_one({"_id": gap_id})["status"] == "resolved"


def test_backtest_revokes_with_one_state_transition(monkeypatch):
    from app.evolution import governance, repositories
    from app.evolution.backtest import runner
    from app.shared.config import settings
    db = mongomock.MongoClient().db
    monkeypatch.setattr(repositories, "get_collection", lambda name: db[name])
    monkeypatch.setattr(settings.evolution, "write_enabled", True)
    monkeypatch.setattr(settings.evolution, "backtest_min_hits", 2)
    monkeypatch.setattr(governance, "index_deactivate", lambda _: True)
    candidate = repositories.evolution_repo.k_candidates
    oid = candidate.insert_one({"status": "active", "publication_status": "active", "evo_doc_id": "evo_cleanup"}).inserted_id
    repositories.evolution_repo.fb_events.insert_many([
        {"ts": time.time(), "cited_chunk_ids": ["evo_cleanup"], "thumbs": -1} for _ in range(2)
    ])
    transitions = []
    for method in ("update_one", "find_one_and_update"):
        original = getattr(candidate, method)
        def counted(query, patch, *args, _original=original, **kwargs):
            if patch.get("$set", {}).get("status") == "deprecated":
                transitions.append(patch["$set"])
            return _original(query, patch, *args, **kwargs)
        monkeypatch.setattr(candidate, method, counted)
    result = runner.run_backtest()
    assert result[0].verdict == "remove"
    assert candidate.find_one({"_id": oid})["publication_status"] == "revoked"
    assert len(transitions) == 1
    task = repositories.evolution_repo.publication_tasks.find_one({"_id": "revoke:evo_cleanup"})
    assert task["state"] == "completed"
