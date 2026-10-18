"""内部试用的问答指标。解决率仅使用用户明确的解决反馈。"""
from __future__ import annotations

from collections import Counter
import math
import time
from statistics import mean

from app.shared.clients.mongo import get_collection
from app.shared.config import settings


def percentile(values, q):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * q) - 1)]


def report(window_hours: int = 24):
    since = time.time() - window_hours * 3600
    runs = list(get_collection(settings.mongo.query_runs_collection).find({"ts": {"$gte": since}}).limit(10000))
    feedback = list(get_collection(settings.mongo.query_feedback_collection).find({"ts": {"$gte": since}}).limit(10000))
    issues = list(get_collection(settings.mongo.query_issues_collection).find({"created_at": {"$gte": since}}).limit(10000))
    rag = [r for r in runs if r.get("intent") == "rag"]
    known_answers = {r.get("answer_id") for r in rag if r.get("answer_id")}
    solved = {f.get("answer_id") for f in feedback if f.get("answer_id") in known_answers and f.get("resolved") is True}
    explicit = {f.get("answer_id") for f in feedback if f.get("answer_id") in known_answers and isinstance(f.get("resolved"), bool)}
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
                         "cost_usd": sum(costs) if len(costs) == len(group) else None}
    total = len(rag)
    return {"window_hours": window_hours, "sample_limit": 10000, "rag_queries": total,
            "resolution_confirmed_count": len(solved),
            "resolution_feedback_count": len(explicit),
            "resolution_rate": len(solved) / total if total else None,
            "resolution_feedback_coverage": len(explicit) / total if total else None,
            "repeat_query_rate": sum(n - 1 for n in repetitions.values()) / total if total else None,
            "handoff_rate": sum(bool(r.get("handoff")) for r in rag) / total if total else None,
            "knowledge_completion_mean_hours": mean(completed) if completed else None,
            "routes": routes}
