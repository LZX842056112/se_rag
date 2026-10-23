"""对已保存的答案打分。缺失的指标为 null，绝不臆造为 0。"""
from __future__ import annotations

import asyncio
from collections import Counter
import math
from numbers import Real
from statistics import mean

METRICS = ("faithfulness", "answer_relevancy", "factual_correctness")


def validate_snapshot_rows(cases: list[dict], rows: list[dict]) -> dict[str, dict]:
    if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
        raise ValueError("快照结果必须是对象列表")
    by_id = {}
    for row in rows:
        key = row.get("case_id")
        if not isinstance(key, str) or key in by_id:
            raise ValueError("快照样本 ID 无效或重复")
        if not isinstance(row.get("response"), str):
            raise ValueError("快照答案必须是字符串")
        contexts = row.get("retrieved_contexts")
        if not isinstance(contexts, list) or any(not isinstance(c, str) for c in contexts):
            raise ValueError("快照证据必须是字符串列表")
        by_id[key] = row
    if set(by_id) != {c["case_id"] for c in cases}:
        raise ValueError("快照与题库的样本 ID 必须完全一致")
    if any(by_id[c["case_id"]].get("query") != c["query"] for c in cases):
        raise ValueError("快照问题与题库不一致")
    return by_id


def _unscored(reason: str, status: str = "not_evaluated") -> dict:
    return {"value": None, "status": status, "reason": reason}


async def _score(cases, by_id, scorers, timeout_seconds):
    results = []
    for case in cases:
        snapshot = by_id[case["case_id"]]
        if snapshot.get("error"):
            status = "query_error"
        elif snapshot.get("actual_intent") != "rag":
            status = "routing_error"
        elif not snapshot["response"].strip():
            status = "empty_answer"
        else:
            status = "completed"
        row = {**snapshot, "product": case["product"], "category": case["category"],
               "reference": case.get("reference", ""), "source": case["source"],
               "annotation_status": case["annotation_status"], "query_status": status, "metrics": {}}
        for name in METRICS:
            if status != "completed":
                row["metrics"][name] = _unscored(status)
                continue
            if name == "faithfulness" and not any(c.strip() for c in snapshot["retrieved_contexts"]):
                row["metrics"][name] = _unscored("missing_contexts")
                continue
            if name == "factual_correctness" and not row["reference"].strip():
                row["metrics"][name] = _unscored("missing_reference")
                continue
            if name not in scorers:
                row["metrics"][name] = _unscored("metric_unavailable", "error")
                continue
            if name == "faithfulness":
                arguments = {"user_input": case["query"], "response": snapshot["response"],
                             "retrieved_contexts": snapshot["retrieved_contexts"]}
            elif name == "answer_relevancy":
                arguments = {"user_input": case["query"], "response": snapshot["response"]}
            else:
                arguments = {"response": snapshot["response"], "reference": row["reference"]}
            try:
                result = await asyncio.wait_for(scorers[name].ascore(**arguments), timeout=timeout_seconds)
                value = result.value
                minimum = -1 if name == "answer_relevancy" else 0
                if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value) or not minimum <= value <= 1:
                    row["metrics"][name] = _unscored("invalid_score", "error")
                else:
                    row["metrics"][name] = {"value": float(value), "status": "scored", "reason": None}
            except Exception as exc:
                row["metrics"][name] = _unscored(type(exc).__name__, "error")
        results.append(row)
    return results


def score_snapshots(cases, rows, scorers, *, timeout_seconds: float = 120, close=None) -> dict:
    by_id = validate_snapshot_rows(cases, rows)
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("评分超时必须大于零")
    async def run():
        try:
            return await _score(cases, by_id, scorers, timeout_seconds)
        finally:
            if close is not None:
                await close()
    results = asyncio.run(run())
    metric_summary = {}
    reasons = Counter()
    for name in METRICS:
        values = [r["metrics"][name]["value"] for r in results if r["metrics"][name]["value"] is not None]
        metric_summary[name] = {"mean": mean(values) if values else None,
                                "valid_count": len(values), "coverage": len(values) / len(cases)}
        reasons.update(r["metrics"][name]["reason"] for r in results if r["metrics"][name]["reason"])
    counts = Counter(r["query_status"] for r in results)
    return {"summary": {"case_count": len(cases), "query_errors": counts["query_error"],
                         "routing_errors": counts["routing_error"], "empty_answers": counts["empty_answer"],
                         "metrics": metric_summary, "unscored_reasons": dict(reasons)},
            "review_queue": [r["case_id"] for r in results if r["annotation_status"] != "reviewed"
                             or any(r["metrics"][n]["value"] is None for n in METRICS)],
            "results": results}


def compare_baseline(report: dict, baseline: dict) -> dict:
    for key in ("dataset_sha256", "documents", "scoring_config"):
        if key not in report or report[key] != baseline.get(key):
            raise ValueError(f"基线不兼容：{key} 不一致")
    if report.get("approved_faq") != baseline.get("approved_faq"):
        raise ValueError("基线不兼容：approved_faq 不一致")
    if not report.get("ready_for_baseline") or not baseline.get("ready_for_baseline"):
        raise ValueError("仅已复核且完整评分的报告可以作为可信基线")
    comparison = {}
    for name, current in report["summary"]["metrics"].items():
        previous = baseline["summary"]["metrics"].get(name, {})
        comparison[name] = {
            "mean_delta": current["mean"] - previous["mean"] if current["mean"] is not None and previous.get("mean") is not None else None,
            "coverage_delta": current["coverage"] - previous.get("coverage", 0),
        }
    return comparison