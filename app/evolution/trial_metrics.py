"""内部试用的问答指标。解决率仅使用用户明确的解决反馈。"""
from __future__ import annotations

from collections import Counter
import time
from statistics import mean

from app.shared.clients.mongo import get_collection
from app.shared.config import settings
from app.shared.utils.statistics import percentile


def report(window_hours: int = 24):
    since = time.time() - window_hours * 3600
    runs = list(get_collection(settings.mongo.query_runs_collection).find({"ts": {"$gte": since}}).limit(10000))
    feedback = list(get_collection(settings.mongo.query_feedback_collection).find({"ts": {"$gte": since}}).limit(10000))
    issues = list(get_collection(settings.mongo.query_issues_collection).find({"created_at": {"$gte": since}}).limit(10000))
    rag = [r for r in runs if r.get("intent") == "rag"]
    known_answers = {r.get("answer_id") for r in rag if r.get("answer_id")}
    feedback_rows = [f for f in feedback if f.get("answer_id") in known_answers]
    solved = {f.get("answer_id") for f in feedback_rows if f.get("resolved") is True}
    explicit = {f.get("answer_id") for f in feedback_rows
                if isinstance(f.get("resolved"), bool) or f.get("thumbs") in (-1, 1)}
    downvoted = {f.get("answer_id") for f in feedback_rows if f.get("thumbs") == -1}
    upvoted = {f.get("answer_id") for f in feedback_rows if f.get("thumbs") == 1}
    repetitions = Counter((r.get("session_id"), r.get("query_hash")) for r in rag)
    completed = [(i["completed_at"] - i["created_at"]) / 3600 for i in issues
                 if i.get("issue_type") == "missing_material" and i.get("completed_at")]
    routes = {}
    for route in ("rag", "chitchat", "degraded"):
        group = [r for r in runs if r.get("degraded")] if route == "degraded" else [r for r in runs if r.get("intent") == route and not r.get("degraded")]
        if not group:
            continue
        durations = [r["duration_ms"] for r in group]
        ttfts = [r["first_token_ms"] for r in group if r.get("first_token_ms") is not None]
        costs = [r["cost_usd"] for r in group if r.get("cost_usd") is not None]
        routes[route] = {"count": len(group), "duration_p50_ms": percentile(durations, .5),
                         "duration_p95_ms": percentile(durations, .95), "first_token_p95_ms": percentile(ttfts, .95),
                         "mean_llm_calls": mean(r.get("llm_calls", 0) for r in group),
                         "input_tokens_mean": mean(r.get("input_tokens", 0) for r in group),
                         "output_tokens_mean": mean(r.get("output_tokens", 0) for r in group),
                         "cost_usd": sum(costs) if len(costs) == len(group) else None,
                         "cost_coverage": len(costs) / len(group)}
    total = len(rag)
    return {"window_hours": window_hours, "sample_limit": 10000, "rag_queries": total,
            "resolution_confirmed_count": len(solved),
            "resolution_feedback_count": len(explicit),
            "resolution_rate": len(solved) / total if total else None,
            "resolution_feedback_coverage": len(explicit) / total if total else None,
            "downvote_count": len(downvoted),
            "downvote_rate": len(downvoted) / total if total else None,
            "downvote_feedback_coverage": len(downvoted) / len(explicit) if explicit else None,
            "upvote_count": len(upvoted),
            "repeat_query_rate": sum(n - 1 for n in repetitions.values()) / total if total else None,
            "handoff_rate": sum(bool(r.get("handoff")) for r in rag) / total if total else None,
            "case_id_coverage": sum(bool(r.get("case_id")) for r in rag) / total if total else None,
            "feedback_case_id_coverage": (
                sum(bool(f.get("case_id")) for f in feedback_rows) / len(feedback_rows)
                if feedback_rows else None),
            "input_tokens_total": sum(r.get("input_tokens", 0) for r in rag),
            "output_tokens_total": sum(r.get("output_tokens", 0) for r in rag),
            "knowledge_completion_mean_hours": mean(completed) if completed else None,
            "routes": routes}
