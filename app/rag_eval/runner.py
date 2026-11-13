"""批量评测执行器：HTTP 取答案 → 检索指标 → RAGAS 生成指标 → 汇总。

并发只用于 HTTP 请求；RAGAS 评分逐题串行，避免 judge 模型被并发打满。
"""
from __future__ import annotations

import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Callable

from app.rag_eval.config import EvalConfig
from app.rag_eval.dataset import EvalCase, load_cases
from app.rag_eval.http_client import EvalHTTPError, QueryClient
from app.rag_eval.ragas_metrics import RagasScorer, aggregate_ragas
from app.rag_eval.retrieval_metrics import aggregate, evaluate_case

Progress = Callable[[str], None]

# 总体结论阈值：用于把结果直接判成 passed / warning / failed，而不是只给一堆数字
RECALL_TARGET = 0.8
MUST_HIT_TARGET = 1.0


def run_evaluation(config: EvalConfig, progress: Progress | None = None) -> dict:
    """执行一次完整评测并返回报告字典。"""
    say = progress or (lambda _message: None)
    cases = [case for case in load_cases(config.dataset_path) if config.selected(list(case.tags))]
    if not cases:
        raise ValueError("按当前标签过滤后没有可执行样本")

    client = QueryClient(config.base_url, config.timeout_seconds, config.retries)
    say(f"健康检查：{config.base_url}")
    client.health()

    say(f"开始评测：{len(cases)} 题，并发 {config.concurrency}")
    answers = _fetch_answers(client, cases, config, say)

    scorer = None
    if config.run_ragas:
        say("初始化 RAGAS judge")
        scorer = RagasScorer(_settings())
        say(f"judge 模型：{scorer.metadata.get('judge_model')}")

    rows: list[dict] = []
    try:
        for index, case in enumerate(cases, start=1):
            row = answers[case.case_id]
            if row.get("error"):
                row["status"] = "failed"
                rows.append(row)
                say(f"[{index}/{len(cases)}] {case.case_id} 失败：{row['error']}")
                continue
            row["status"] = "succeeded"
            row["retrieval"] = evaluate_case(row.pop("retrieval_layers"), case, config.ks)
            if scorer is not None:
                row["ragas"] = scorer.score(
                    question=case.question,
                    answer=row["answer"],
                    contexts=row["contexts"],
                    ground_truth=case.ground_truth,
                )
            rows.append(row)
            say(f"[{index}/{len(cases)}] {case.case_id} {_case_brief(row, config.ks)}")
    finally:
        client.close()
        if scorer is not None:
            scorer.close()

    return _build_report(config, cases, rows, scorer)


def _settings():
    from app.shared.config import settings

    return settings


def _fetch_answers(
    client: QueryClient, cases: list[EvalCase], config: EvalConfig, say: Progress
) -> dict[str, dict]:
    """并发请求所有题目，返回 ``case_id -> 原始结果``。"""
    results: dict[str, dict] = {}

    def ask(case: EvalCase) -> tuple[str, dict]:
        try:
            result = client.ask(case.question)
            return case.case_id, {
                "case_id": case.case_id,
                "question": case.question,
                "answer": result.answer,
                "contexts": result.contexts,
                "evidence_count": len(result.evidence),
                "citations": len(result.citations),
                "item_names": result.item_names,
                "intent": result.intent,
                "groundedness": result.groundedness,
                "latency_ms": result.latency_ms,
                "retrieval_layers": result.retrieval_layers,
                "error": "",
            }
        except EvalHTTPError as exc:
            return case.case_id, {
                "case_id": case.case_id,
                "question": case.question,
                "error": str(exc),
                "latency_ms": 0.0,
            }

    with ThreadPoolExecutor(max_workers=max(1, config.concurrency)) as pool:
        for case_id, row in pool.map(ask, cases):
            results[case_id] = row
    say(f"请求完成：{sum(1 for row in results.values() if not row.get('error'))} 题成功")
    return results


def _build_report(
    config: EvalConfig, cases: list[EvalCase], rows: list[dict], scorer
) -> dict:
    """组装报告：元信息 + 汇总 + 逐题结果。"""
    successful = [row for row in rows if not row.get("error")]
    latencies = [row["latency_ms"] for row in successful if row.get("latency_ms")]
    summary = {
        "total": len(cases),
        "succeeded": len(successful),
        "failed": len(cases) - len(successful),
        "retrieval": aggregate(successful, config.ks),
        "latency_ms": _latency_summary(latencies),
    }
    if config.run_ragas:
        summary["ragas"] = aggregate_ragas(successful)

    summary["conclusion"] = _conclusion(summary, config.ks)
    return {
        "meta": {
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "base_url": config.base_url,
            "dataset": str(config.dataset_path),
            "ks": list(config.ks),
            "ragas_enabled": config.run_ragas,
            "judge": (scorer.metadata if scorer is not None else {}),
        },
        "summary": summary,
        "cases": sorted(rows, key=lambda row: row["case_id"]),
    }


def _primary_k(ks: tuple[int, ...]) -> int:
    """结论默认看 @k：优先 5，否则取配置的最大 k。"""
    return 5 if 5 in ks else max(ks)


def _conclusion(summary: dict, ks: tuple[int, ...]) -> dict:
    """把汇总结果翻译成可直接阅读的结论与原因。"""
    k = _primary_k(ks)
    reranked = (summary.get("retrieval") or {}).get("reranked") or {}
    recall = reranked.get(f"recall@{k}")
    must_hit = reranked.get(f"must_hit@{k}")
    ndcg = reranked.get(f"ndcg@{k}")

    reasons: list[str] = []
    if summary.get("failed"):
        reasons.append(f"{summary['failed']} 题执行失败")
    if isinstance(recall, (int, float)) and recall < RECALL_TARGET:
        reasons.append(f"reranked recall@{k}={recall} 低于目标 {RECALL_TARGET}")
    if isinstance(must_hit, (int, float)) and must_hit < MUST_HIT_TARGET:
        reasons.append(f"reranked must_hit@{k}={must_hit} 低于目标 {MUST_HIT_TARGET}")

    if summary.get("failed"):
        status = "failed"
    elif recall is None and must_hit is None:
        # 没有标注就无法判断检索质量；不能默认算「通过」
        status = "inconclusive"
        reasons.append("本次样本没有可计算的检索指标（未标注 gold_chunk_ids）")
    elif reasons:
        status = "warning"
    else:
        status = "passed"

    ragas = summary.get("ragas") or {}
    return {
        "status": status,
        "reasons": reasons or ["全部样本执行成功，关键检索指标达到目标"],
        "key_metrics": {
            f"recall@{k}": recall,
            f"must_hit@{k}": must_hit,
            f"ndcg@{k}": ndcg,
            "faithfulness": ragas.get("faithfulness"),
            "answer_relevancy": ragas.get("answer_relevancy"),
            "latency_ms_avg": (summary.get("latency_ms") or {}).get("avg"),
        },
    }


def _case_brief(row: dict, ks: tuple[int, ...]) -> str:
    """单题完成时打印一行关键结果，避免结果只留在 JSON 里。"""
    k = _primary_k(ks)
    reranked = (row.get("retrieval") or {}).get("reranked") or {}
    recall = reranked.get(f"recall@{k}")
    must_hit = reranked.get(f"must_hit@{k}")

    def _show(value):
        return "无标注" if value is None else value

    parts = [f"recall@{k}={_show(recall)}", f"must_hit@{k}={_show(must_hit)}"]
    ragas = row.get("ragas") or {}
    if isinstance(ragas.get("faithfulness"), (int, float)):
        parts.append(f"faithfulness={ragas['faithfulness']}")
    parts.append(f"{row.get('latency_ms')}ms")
    return " | ".join(parts)


def _latency_summary(values: list[float]) -> dict:
    if not values:
        return {"avg": None, "p95": None}
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))
    return {
        "avg": round(statistics.mean(ordered), 1),
        "p95": round(ordered[index], 1),
    }


def timed_run(config: EvalConfig, progress: Progress | None = None) -> dict:
    """带总耗时的运行入口（CLI 使用）。"""
    started = time.perf_counter()
    report = run_evaluation(config, progress)
    report["meta"]["duration_seconds"] = round(time.perf_counter() - started, 1)
    return report
