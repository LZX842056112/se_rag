"""完整 HTTP 入口评测。默认只校验样本。真实执行需明确允许外部调用。"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
from statistics import mean
import time
import uuid


def validate_cases(cases: list[dict]) -> dict:
    ids = set()
    for case in cases:
        if not case.get("case_id") or case["case_id"] in ids:
            raise ValueError("样本 ID 缺失或重复")
        ids.add(case["case_id"])
        if not isinstance(case.get("query"), str) or not case["query"].strip():
            raise ValueError("样本缺少问题")
        if case.get("expected_intent") not in ("rag", "chitchat"):
            raise ValueError("样本意图标签不合法")
        if not all(isinstance(q, str) for q in case.get("history", [])):
            raise ValueError("history 必须为按顺序排列的用户提问列表")
    reviewed = sum(c.get("annotation_status") == "reviewed" and bool(c.get("reviewed_by", "").strip()) for c in cases)
    return {"case_count": len(cases), "reviewed_count": reviewed,
            "categories": dict(Counter(c.get("category", "other") for c in cases)),
            "ready_for_acceptance": len(cases) >= 200 and reviewed == len(cases)}


def _p95(values):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, __import__("math").ceil(len(ordered) * .95) - 1)]


def evaluate_cases(cases, client, *, revision: str):
    info = validate_cases(cases)
    results = []
    for case in cases:
        session = "eval-" + uuid.uuid4().hex
        seed_calls = 0
        row = {"case_id": case["case_id"], "expected_intent": case["expected_intent"],
               "critical": bool(case.get("critical")), "actual_intent": None}
        try:
            for question in case.get("history", []):
                response = client.post("/api/query", json={"query": question, "session_id": session, "is_stream": False})
                response.raise_for_status()
                seed_calls += 1
            started = time.perf_counter()
            response = client.post("/api/query", json={"query": case["query"], "session_id": session, "is_stream": False})
            response.raise_for_status()
            payload = response.json()
            expected_items = set(case.get("expected_items") or [])
            row.update({"actual_intent": payload.get("intent"), "elapsed_ms": (time.perf_counter() - started) * 1000,
                        "seed_requests": seed_calls, "answer": payload.get("answer", ""),
                        "subject_correct": expected_items.issubset(set(payload.get("item_names") or [])) if expected_items else None,
                        "answer_check_passed": all(term in payload.get("answer", "") for term in case["must_contain"]) if case.get("must_contain") else None,
                        "metrics": payload.get("metrics") or {}, "error": None})
        except Exception as exc:
            row["error"] = type(exc).__name__
        results.append(row)
    knowledge = [r for r in results if r["expected_intent"] == "rag"]
    chitchat_predicted = [r for r in results if r["actual_intent"] == "chitchat"]
    recall = sum(r["actual_intent"] == "rag" for r in knowledge) / len(knowledge) if knowledge else None
    critical_misses = [r["case_id"] for r in knowledge if r["critical"] and r["actual_intent"] == "chitchat"]
    subject = [r["subject_correct"] for r in results if r.get("subject_correct") is not None]
    checks = [r["answer_check_passed"] for r in results if r.get("answer_check_passed") is not None]
    times = [r["elapsed_ms"] for r in results if r.get("elapsed_ms") is not None]
    ttfts = [r["metrics"]["first_token_ms"] for r in results if r.get("metrics", {}).get("first_token_ms") is not None]
    costs = [r["metrics"]["cost_usd"] for r in results if r.get("metrics", {}).get("cost_usd") is not None]
    errors = sum(bool(r.get("error")) for r in results)
    summary = {"knowledge_recall": recall, "critical_false_chitchat": critical_misses,
               "chitchat_precision": sum(r["expected_intent"] == "chitchat" for r in chitchat_predicted) / len(chitchat_predicted) if chitchat_predicted else None,
               "subject_accuracy": mean(subject) if subject else None,
               "answer_check_rate": mean(checks) if checks else None,
               "answer_check_coverage": len(checks) / len(results) if results else None,
               "duration_p95_ms": _p95(times), "first_token_p95_ms": _p95(ttfts),
               "mean_llm_calls": mean(r.get("metrics", {}).get("llm_calls", 0) for r in results) if results else None,
               "cost_usd": sum(costs) if len(costs) == len(results) and results else None, "http_errors": errors}
    digest = hashlib.sha256(json.dumps(cases, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return {"revision": revision, "dataset_sha256": digest, "dataset": info, "summary": summary,
            "routing_gate_passed": info["ready_for_acceptance"] and recall is not None and recall >= .99 and not critical_misses and errors == 0,
            "answer_quality_requires_review": len(checks) != len(results), "results": results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=Path(__file__).with_name("artifacts") / "intent_trial_draft.json")
    parser.add_argument("--base-url", default="http://127.0.0.1:8001")
    parser.add_argument("--output", type=Path, default=Path("output/full-flow-report.json"))
    parser.add_argument("--revision", default="unspecified")
    parser.add_argument("--allow-external", action="store_true")
    parser.add_argument("--allow-unreviewed", action="store_true")
    parser.add_argument("--baseline", type=Path)
    args = parser.parse_args()
    cases = json.loads(args.cases.read_text(encoding="utf-8"))
    info = validate_cases(cases)
    if not args.allow_external:
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return
    if not info["ready_for_acceptance"] and not args.allow_unreviewed:
        parser.error("真实评测前请人工复核至少200条样本。调试可用 --allow-unreviewed，但不能通过验收门槛。")
    import httpx
    with httpx.Client(base_url=args.base_url, timeout=300) as client:
        report = evaluate_cases(cases, client, revision=args.revision)
    if args.baseline:
        baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
        if baseline.get("dataset_sha256") != report["dataset_sha256"]:
            parser.error("基线与本轮必须使用同一份已复核题库")
        report["comparison"] = {key: report["summary"][key] - baseline["summary"][key]
                                for key in ("knowledge_recall", "subject_accuracy", "answer_check_rate", "duration_p95_ms", "mean_llm_calls", "cost_usd")
                                if report["summary"].get(key) is not None and baseline.get("summary", {}).get(key) is not None}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    print(str(args.output.resolve()))


if __name__ == "__main__":
    main()
