"""通过 HTTP 运行评测。按题检查行为、检索和 RAGAS 指标。"""
from __future__ import annotations

import statistics
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Callable

from app.rag_eval.config import EvalConfig
from app.rag_eval.dataset import EvalCase, load_dataset
from app.rag_eval.http_client import EvalHTTPError, QueryClient
from app.rag_eval.ragas_metrics import METRIC_NAMES, RagasScorer, aggregate_ragas, finite_score
from app.rag_eval.retrieval_metrics import aggregate, evaluate_case

Progress = Callable[[str], None]
METRIC_SCHEMA_VERSION = 3
GATE_K = 5
RECALL_TARGET = 0.8
MUST_HIT_TARGET = 1.0
_SERVER_TIMING_FIELDS = ("duration_ms", "first_token_ms", "classification_ms", "queue_ms")
_SERVER_METRIC_FIELDS = (
    *_SERVER_TIMING_FIELDS, "stage_ms", "degraded", "degraded_reasons", "rerank_pairs", "rerank_input_tokens",
)


def run_evaluation(config: EvalConfig, progress: Progress | None = None) -> dict:
    """执行一次评测并返回报告。"""
    say = progress or (lambda _message: None)
    dataset, dataset_sha256 = load_dataset(config.dataset_path)
    cases = [case for case in dataset if config.selected(list(case.tags))]
    if not cases:
        raise ValueError("按当前标签过滤后没有可执行样本")
    ks = tuple(sorted(set(config.ks) | {GATE_K}))

    client = QueryClient(config.base_url, config.timeout_seconds, config.retries)
    try:
        say(f"健康检查 {config.base_url}")
        server = client.health().get("evaluation") or {}
        say(f"开始请求 {len(cases)} 题，并发 {config.concurrency}")
        answers = _fetch_answers(client, cases, config, say)
    finally:
        client.close()

    rows = [answers[case.case_id] for case in cases]
    for case, row in zip(cases, rows):
        _prepare_case(case, row, ks, config.run_ragas)
    judge = _score_cases(cases, rows, say)
    return _build_report(config, rows, ks, server, judge, dataset_sha256)


def _settings():
    from app.shared.config import settings

    return settings


def _fetch_answers(
    client: QueryClient, cases: list[EvalCase], config: EvalConfig, say: Progress,
) -> dict[str, dict]:
    def ask(case: EvalCase) -> dict:
        started = time.perf_counter()
        try:
            result = client.ask(case.question)
        except EvalHTTPError as exc:
            return {
                "case_id": case.case_id, "question": case.question, "status": "failed",
                "error": str(exc) or type(exc).__name__,
                "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                "http_attempts": exc.http_attempts,
                "http_failure_reasons": list(exc.http_failure_reasons),
            }

        return {
            "case_id": case.case_id,
            "question": case.question,
            "status": "succeeded",
            "error": "",
            "answer": result.answer,
            "contexts": result.contexts,
            "evidence_count": len(result.evidence),
            "context_count": len(result.contexts),
            "citations": len(result.citations),
            "item_names": result.item_names,
            "intent": result.intent,
            "outcome": result.outcome,
            "query_mode": result.query_mode,
            "evaluation_schema_version": result.evaluation_schema_version,
            "groundedness": result.groundedness,
            "latency_ms": result.latency_ms,
            "http_attempts": result.http_attempts,
            "http_failure_reasons": list(result.http_failure_reasons),
            "server_metrics": {
                key: result.server_metrics[key]
                for key in _SERVER_METRIC_FIELDS
                if key in result.server_metrics
            },
            "retrieval_layers": result.retrieval_layers,
            "candidate_counts": {name: len(ids) for name, ids in result.retrieval_layers.items()},
        }

    results: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=max(1, config.concurrency)) as pool:
        futures = [pool.submit(ask, case) for case in cases]
        for index, future in enumerate(as_completed(futures), start=1):
            row = future.result()
            case_id = row["case_id"]
            results[case_id] = row
            detail = row["error"] or f"{row['outcome'] or '未知结果'} | {row['latency_ms']} ms"
            say(f"[HTTP {index}/{len(cases)}] {case_id} {detail}")
    return results


def _prepare_case(case: EvalCase, row: dict, ks: tuple[int, ...], run_ragas: bool) -> None:
    expected = case.expected_outcome or "answered"
    behavior = {
        "expected_outcome": expected,
        "expected_item_names": list(case.expected_item_names),
        "required_answer_terms": list(case.required_answer_terms),
        "forbidden_answer_terms": list(case.forbidden_answer_terms),
        "explicit": bool(case.expected_outcome or case.expected_item_names
                         or case.required_answer_terms or case.forbidden_answer_terms),
        "status": "not_run",
        "reasons": [],
    }
    row["behavior"] = behavior
    row["answerable"] = expected == "answered"
    row["required_retrieval"] = [
        name for name, ids in (
            ("recall@5", case.gold_chunk_ids), ("must_hit@5", case.must_hit_chunk_ids),
        )
        if row["answerable"] and ids
    ]
    if row["status"] == "failed":
        row["scoring_status"] = "http_failed"
        return

    answer = row["answer"].strip().casefold()
    reasons = behavior["reasons"]
    if not answer:
        reasons.append("回答为空")
    if row["outcome"] != expected:
        reasons.append(f"预期结果为 {expected}，实际结果为 {row['outcome'] or '缺失'}")
    if case.expected_item_names:
        expected_names = {name.casefold() for name in case.expected_item_names}
        actual_names = {name.strip().casefold() for name in row["item_names"]}
        if actual_names != expected_names:
            reasons.append(f"主体不符。预期 {list(case.expected_item_names)}，实际 {row['item_names']}")
    missing = [term for term in case.required_answer_terms if term.casefold() not in answer]
    if missing:
        reasons.append(f"缺少必需词 {missing}")
    forbidden = [term for term in case.forbidden_answer_terms if term.casefold() in answer]
    if forbidden:
        reasons.append(f"包含禁用词 {forbidden}")
    behavior["status"] = "failed" if reasons else "passed"

    if not row["answerable"]:
        status = "behavior_failed" if reasons else "expected_no_score"
    else:
        row["retrieval"] = evaluate_case(row["retrieval_layers"], case, ks)
        if not answer:
            status = "missing_answer"
        elif not row["contexts"]:
            status = "missing_context"
        elif row["outcome"] != expected:
            status = "unexpected_outcome"
        else:
            status = "ready" if run_ragas else "disabled"
    row["scoring_status"] = status


def _score_cases(cases: list[EvalCase], rows: list[dict], say: Progress) -> dict:
    pending = [(case, row) for case, row in zip(cases, rows) if row["scoring_status"] == "ready"]
    if not pending:
        return {}
    say("初始化 RAGAS judge")
    try:
        scorer = RagasScorer(_settings())
    except Exception as exc:  # noqa: BLE001 - 保留已取得的查询结果
        error = f"RAGAS 初始化失败。{exc}"
        say(error)
        for _, row in pending:
            row["ragas"] = {"error": error}
            row["scoring_status"] = "scorer_failed"
        return {}

    try:
        say(f"judge 模型 {scorer.metadata.get('judge_model')}")
        for index, (case, row) in enumerate(pending, start=1):
            try:
                scores = scorer.score(
                    question=case.question, answer=row["answer"],
                    contexts=row["contexts"], ground_truth=case.ground_truth,
                )
            except Exception as exc:  # noqa: BLE001 - 保留其余题目的评分
                scores = {"error": str(exc) or type(exc).__name__}
            clean_scores = {name: scores[name] if finite_score(scores.get(name)) else None for name in METRIC_NAMES}
            missing = [name for name, value in clean_scores.items() if value is None]
            row["ragas"] = clean_scores
            if scores.get("retries"):
                row["ragas_retries"] = scores["retries"]
            if missing:
                row["ragas"]["error"] = scores.get("error") or f"缺少有效分数 {', '.join(missing)}"
            row["scoring_status"] = "scorer_failed" if missing else "scored"
            say(f"[RAGAS {index}/{len(pending)}] {case.case_id} {row['scoring_status']}")
        return scorer.metadata
    finally:
        scorer.close()


def _build_report(
    config: EvalConfig, rows: list[dict], ks: tuple[int, ...], server: dict, judge: dict, dataset_sha256: str,
) -> dict:
    successful = [row for row in rows if row["status"] == "succeeded"]
    answerable = [row for row in rows if row["answerable"]]
    retrieval = aggregate(successful, ks)
    final = retrieval.get("final", {})
    ragas = aggregate_ragas(successful) if config.run_ragas else {}

    coverage = {
        "http": {"required": len(rows), "completed": len(successful)},
        "behavior": {
            "required": len(rows), "completed": sum(row["behavior"]["status"] == "passed" for row in rows),
            "failed": sum(row["behavior"]["status"] == "failed" for row in rows),
        },
        "answer_context": {
            "required": len(answerable),
            "completed": sum(row["scoring_status"] in ("disabled", "scored", "scorer_failed") for row in answerable),
        },
        "retrieval": {
            name: {
                "required": sum(name in row["required_retrieval"] for row in answerable),
                "completed": final.get(f"{name}_n", 0),
            }
            for name in ("recall@5", "must_hit@5")
        },
        "ragas": {
            name: {"required": len(answerable), "completed": ragas.get(f"{name}_n", 0)}
            for name in METRIC_NAMES
        } if config.run_ragas else {},
    }
    summary = {
        "total": len(rows),
        "succeeded": len(successful),
        "failed": len(rows) - len(successful),
        "coverage": coverage,
        "query_modes": dict(Counter(row["query_mode"] or "unknown" for row in successful)),
        "outcomes": dict(Counter(row["outcome"] or "unknown" for row in successful)),
        "scoring_statuses": dict(Counter(row["scoring_status"] for row in rows)),
        "degradation": _degradation_summary(successful),
        "retrieval": retrieval,
        "latency_ms": _latency_summary([row["latency_ms"] for row in successful]),
        "server_timing_ms": _server_timing(successful),
    }
    attempts = [row["http_attempts"] for row in rows if "http_attempts" in row]
    if attempts:
        summary["http_attempts"] = {
            "total": sum(attempts),
            "retried_cases": sum(attempt > 1 for attempt in attempts),
            "failed_attempts_by_reason": dict(Counter(
                reason for row in rows for reason in row.get("http_failure_reasons", ())
            )),
        }
    if config.run_ragas:
        summary["ragas"] = ragas
        retries = Counter(retry["metric"] for row in rows for retry in row.get("ragas_retries", ()))
        if retries:
            summary["ragas_retries"] = dict(retries)
    explicit_behavior = any(row["behavior"]["explicit"] for row in rows)
    summary["conclusion"] = _conclusion(summary, explicit_behavior)
    return {
        "meta": {
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "base_url": config.base_url,
            "dataset": str(config.dataset_path),
            "dataset_sha256": dataset_sha256,
            "case_ids": sorted(row["case_id"] for row in rows),
            "tags": sorted(config.tags),
            "ks": list(ks),
            "concurrency": max(1, config.concurrency),
            "timeout_seconds": config.timeout_seconds,
            "retries": config.retries,
            "metric_schema_version": METRIC_SCHEMA_VERSION,
            "evaluation_schema_versions": sorted({row["evaluation_schema_version"] for row in successful
                                                  if row["evaluation_schema_version"] is not None}),
            "server": server if isinstance(server, dict) else {},
            "ragas_enabled": config.run_ragas,
            "judge": judge,
        },
        "summary": summary,
        "cases": sorted(rows, key=lambda row: row["case_id"]),
    }


def _conclusion(summary: dict, explicit_behavior: bool) -> dict:
    coverage = summary["coverage"]
    reasons: list[str] = []
    if summary["failed"]:
        reasons.append(f"{summary['failed']} 题执行失败")
    if coverage["behavior"]["failed"]:
        reasons.append(f"{coverage['behavior']['failed']} 题行为检查失败")
    if coverage["answer_context"]["completed"] < coverage["answer_context"]["required"]:
        reasons.append("部分应回答的用例缺少有效回答或评分上下文")
    for section, label in (("retrieval", "最终证据"), ("ragas", "RAGAS")):
        for name, counts in coverage[section].items():
            if counts["completed"] < counts["required"]:
                reasons.append(f"{label} {name} 缺少分数，已完成 {counts['completed']}/{counts['required']}")

    valid = not reasons
    final = summary["retrieval"].get("final", {})
    recall, must_hit = final.get("recall@5"), final.get("must_hit@5")
    for name, value, target in (("recall@5", recall, RECALL_TARGET), ("must_hit@5", must_hit, MUST_HIT_TARGET)):
        if value is not None and value < target:
            reasons.append(f"final {name}={value} 低于目标 {target}")
    if not valid:
        status = "failed"
    elif reasons:
        status = "warning"
    elif recall is None and must_hit is None and not explicit_behavior and not coverage["ragas"]:
        status = "inconclusive"
        reasons.append("缺少检索标注 gold_chunk_ids、行为标注和 RAGAS 分数")
    else:
        status = "passed"

    ragas = summary.get("ragas", {})
    return {
        "status": status,
        "valid_measurement": valid and status != "inconclusive",
        "reasons": reasons or ["已完成所需检查，关键指标达到目标"],
        "key_metrics": {
            "recall@5": recall,
            "must_hit@5": must_hit,
            "ndcg@5": final.get("ndcg@5"),
            "faithfulness": ragas.get("faithfulness"),
            "answer_relevancy": ragas.get("answer_relevancy"),
            "latency_ms_avg": summary["latency_ms"].get("avg"),
        },
    }


def _latency_summary(values: list[float]) -> dict:
    if not values:
        return {"avg": None, "p95": None}
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))
    return {
        "avg": round(statistics.mean(ordered), 1),
        "p95": round(ordered[index], 1),
    }


def _server_timing(rows: list[dict]) -> dict:
    values: dict[str, list[float]] = {}
    for row in rows:
        metrics = row["server_metrics"]
        stages = metrics.get("stage_ms")
        for name in _SERVER_TIMING_FIELDS:
            value = metrics.get(name)
            if finite_score(value):
                values.setdefault(name, []).append(value)
        if isinstance(stages, dict):
            for name, value in stages.items():
                if finite_score(value):
                    values.setdefault(f"stage.{name}", []).append(value)
    return {name: _latency_summary(samples) | {"n": len(samples)} for name, samples in sorted(values.items())}


def _degradation_summary(rows: list[dict]) -> dict:
    flags = [row["server_metrics"] for row in rows if type(row["server_metrics"].get("degraded")) is bool]
    degraded = [metrics for metrics in flags if metrics["degraded"]]
    reasons: Counter[str] = Counter()
    for metrics in degraded:
        reported = metrics.get("degraded_reasons")
        if isinstance(reported, list):
            reasons.update({reason for reason in reported if isinstance(reason, str) and reason})
    return {"reported": len(flags), "degraded": len(degraded), "by_reason": dict(sorted(reasons.items()))}


def timed_run(config: EvalConfig, progress: Progress | None = None) -> dict:
    """执行评测并记录总耗时。"""
    started = time.perf_counter()
    report = run_evaluation(config, progress)
    report["meta"]["duration_seconds"] = round(time.perf_counter() - started, 1)
    return report
