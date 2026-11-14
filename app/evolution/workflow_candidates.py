"""Build candidate lifecycle states from the committed evolution records."""
from __future__ import annotations

import time

from bson import ObjectId

from app.evolution.feedback.state import is_unresolved, unresolved_filter
from app.evolution.gap.detector import _feedback_gap_id
from app.evolution.gap.registration import VISIBLE_REGISTRATION
from app.evolution.governance import publication_key, revocation_key
from app.evolution.repositories import evolution_repo as repo
from app.shared.config import settings
from app.shared.runtime.workflow_events import ACTIVE, limit_runs

_FEEDBACK = {field: 1 for field in (
    "session_id", "answer_id", "ts", "workflow_entered_at", "resolved", "thumbs", "adopt",
    "workflow_scan_outcome", "workflow_outcome_at", "workflow_outcome_for",
    "gap_scan_id", "gap_scanned_at", "gap_scan_issue_id",
)}
_GAPS = {field: 1 for field in (
    "candidate_id", "source_feedback_id", "source_feedback_keys", "ts", "status", "resume_status", "gap_kind",
    "lease_until", "next_retry_at", "last_attempt_at", "processed_at", "reviewed_at", "resolved_at", "issue_id",
)}
_CANDIDATES = {field: 1 for field in (
    "gap_id", "ts", "status", "publication_status", "content_version", "approved_at", "approved_content_sha256",
    "evo_doc_id", "content_sha256", "manual_edited_at", "workflow_updated_at", "workflow_supplemented_at",
)}


def _task_status(task: dict, now: float) -> str:
    state = task.get("state")
    if state == "running":
        return "running" if _number(task, "lease_until") > now else "retrying"
    return {"retry": "retrying", "cancelled": "failed", "completed": "completed"}.get(state, "waiting")


def _task_key(candidate: dict) -> str | None:
    publication = candidate.get("publication_status")
    if publication in {"revoked", "deprecated"} and candidate.get("evo_doc_id"):
        return revocation_key(candidate["evo_doc_id"])
    if publication in {"indexing", "active"} and candidate.get("approved_content_sha256"):
        return publication_key(str(candidate["_id"]), candidate.get("content_version", 1),
                               candidate["approved_content_sha256"])
    return None


def _number(record: dict, key: str) -> float:
    value = record.get(key)
    return value if isinstance(value, (int, float)) else 0


def _candidate_run(feedback: dict, gap: dict, candidate: dict, task: dict, now: float) -> dict:
    nodes = {"feedback": {"status": "completed" if feedback else "unknown", "visits": int(bool(feedback))}}
    status, detail, attempt = "waiting", None, 1

    def mark(node_id: str, state: str) -> None:
        nodes[node_id] = {"status": state, "visits": int(state in {"completed", "running", "failed"})}

    if gap:
        mark("gap", "completed")
    elif candidate:
        mark("gap", "unknown")

    gap_status = gap.get("status")
    candidate_status = candidate.get("status")
    if candidate:
        mark("candidate", "completed")
        if candidate.get("workflow_supplemented_at"):
            mark("supplement", "completed")
        publication = candidate.get("publication_status")
        if candidate_status == "rejected":
            mark("reject", "completed")
            status = "rejected"
            if candidate.get("evo_doc_id"):
                mark("withdraw", _task_status(task, now) if task else "unknown")
        elif publication in {"revoked", "deprecated"} or candidate_status in {"deprecated", "removed"}:
            if candidate.get("approved_at"):
                mark("approval", "completed")
            if candidate.get("evo_doc_id"):
                digest = candidate.get("approved_content_sha256")
                mark("publish", "completed" if digest and candidate.get("content_sha256") == digest else "unknown")
                mark("withdraw", _task_status(task, now))
            else:
                mark("withdraw", "completed")
            status = {"completed": "withdrawn", "running": "processing", "failed": "failed"}.get(nodes["withdraw"]["status"], nodes["withdraw"]["status"])
        elif candidate_status == "active":
            mark("approval", "completed" if publication == "active" else "unknown")
            mark("publish", "completed" if publication == "active" else "unknown")
            status = "completed"
        elif publication == "indexing":
            mark("approval", "completed")
            phase = _task_status(task, now)
            if phase == "completed":
                phase = "waiting"  # Candidate activation also has to be committed.
            mark("publish", phase)
            status = "processing" if phase == "running" else phase
        elif candidate_status == "need_info":
            mark("supplement", "waiting")
            detail = "need_info"
        else:
            if candidate.get("manual_edited_at"):
                mark("supplement", "completed")
            mark("approval", "waiting")
            detail = "awaiting_approval"
        attempt = task.get("attempts") or 1
    elif gap_status in {"candidate", "reviewing", "processing", "need_info", "draft"}:
        if _number(gap, "next_retry_at") > now or (gap_status == "processing" and _number(gap, "lease_until") <= now):
            mark("candidate", "retrying")
            status = "retrying"
        elif gap_status == "processing" and gap.get("resume_status") == "reviewing":
            mark("candidate", "running")
            status = "processing"
        elif gap_status == "processing":
            target = "candidate" if gap.get("gap_kind") == "knowledge_missing" else "strategy"
            mark(target, "running")
            status = "processing"
        else:
            mark("candidate", "waiting")
    elif gap_status in {"strategy_pending", "pending"}:
        mark("strategy", "waiting")
    elif gap_status in {"dismissed", "resolved", "rejected", "removed", "deprecated"}:
        if gap_status == "resolved" and gap.get("issue_id"):
            mark("strategy", "completed")
        status, detail = "closed", "resolved" if gap_status == "resolved" else "ignored"
    elif feedback.get("gap_scan_issue_id"):
        mark("gap", "completed")
        mark("strategy", "completed")
        status, detail = "closed", "resolved"
    elif feedback and not is_unresolved(feedback):
        status, detail = "closed", "resolved"
    elif feedback.get("workflow_scan_outcome") and feedback.get("workflow_outcome_for") == feedback.get("ts"):
        mark("gap", "completed")
        status, detail = "closed", feedback["workflow_scan_outcome"]
    elif feedback and _number(feedback, "ts") <= now - settings.evolution.observe_window_days * 86400:
        mark("gap", "skipped")
        status, detail = "closed", "outside_window"
    else:
        mark("gap", "waiting")

    if status in {"completed", "closed", "rejected", "withdrawn"}:
        mark("__end__", "completed")
    created = _number(feedback, "workflow_entered_at") or _number(feedback, "ts") or _number(candidate, "ts")
    updated = max(created, *(_number(feedback, key) for key in ("ts", "gap_scanned_at", "workflow_outcome_at")),
                  *(_number(gap, key) for key in ("ts", "reviewed_at", "last_attempt_at", "processed_at", "resolved_at")),
                  *(_number(candidate, key) for key in ("ts", "approved_at", "manual_edited_at", "workflow_updated_at")),
                  *(_number(task, key) for key in ("created_at", "completed_at", "workflow_updated_at")))
    return {"id": str(feedback["_id"]) if feedback else f"candidate:{candidate['_id']}",
            "label": "反馈" if feedback else "候选", "status": status, "detail": detail,
            "nodes": nodes, "attempt": attempt, "revision": 0, "created_at": created, "updated_at": updated}


def list_candidate_runs(*, limit: bool = True) -> list[dict]:
    now = time.time()
    horizon = now - settings.evolution.observe_window_days * 86400
    feedback = list(repo.fb_events.find({"$and": [
        {"$or": [{"ts": {"$gte": horizon}}, {"gap_scan_id": {"$gt": ""}}]},
        {"$or": [unresolved_filter(), {"workflow_entered_at": {"$exists": True}}, {"gap_scanned_at": {"$exists": True}}]},
    ]}, _FEEDBACK))
    keys = {_feedback_gap_id(event) for event in feedback}
    gap_ids = keys | {ObjectId(event["gap_scan_id"]) for event in feedback if ObjectId.is_valid(event.get("gap_scan_id"))}
    gaps = list(repo.k_gaps.find({"$or": [{"_id": {"$in": list(gap_ids)}},
        {"source_feedback_keys": {"$in": [str(key) for key in keys]}},
        {"source_feedback_id": {"$in": [str(event["_id"]) for event in feedback]}}]}, _GAPS))
    by_id = {str(gap["_id"]): gap for gap in gaps}
    by_key = {key: gap for gap in gaps for key in gap.get("source_feedback_keys", [])}
    by_feedback = {gap.get("source_feedback_id"): gap for gap in gaps if gap.get("source_feedback_id")}
    candidates = list(repo.k_candidates.find(VISIBLE_REGISTRATION, _CANDIDATES))
    by_candidate = {str(candidate["_id"]): candidate for candidate in candidates}
    task_keys = {candidate["_id"]: _task_key(candidate) for candidate in candidates}
    wanted = [key for key in task_keys.values() if key]
    tasks = {task["_id"]: task for task in repo.publication_tasks.find({"_id": {"$in": wanted}}, {
        "state": 1, "lease_until": 1, "attempts": 1, "created_at": 1, "completed_at": 1, "workflow_updated_at": 1,
    })} if wanted else {}
    used = set()
    rows = []
    for event in feedback:
        key = str(_feedback_gap_id(event))
        gap = by_id.get(str(event.get("gap_scan_id"))) or by_id.get(key) or by_key.get(key) or by_feedback.get(str(event["_id"])) or {}
        candidate = by_candidate.get(str(gap.get("candidate_id"))) or {}
        if candidate and str(candidate.get("gap_id")) != str(gap["_id"]):
            candidate = {}
        if candidate:
            used.add(candidate["_id"])
        rows.append(_candidate_run(event, gap, candidate, tasks.get(task_keys.get(candidate.get("_id")), {}), now))
    for candidate in candidates:
        if candidate["_id"] not in used:
            rows.append(_candidate_run({}, {}, candidate, tasks.get(task_keys[candidate["_id"]], {}), now))
    cutoff = now - settings.runtime.task_state_ttl_seconds
    retained = [row for row in rows if row["status"] in ACTIVE or row["updated_at"] > cutoff]
    return limit_runs(retained) if limit else retained
