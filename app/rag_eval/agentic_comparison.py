"""对比传统与 agentic 的答案。默认只校验本地文件。"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import json
import math
from pathlib import Path
import re
from statistics import mean, stdev

from app.rag_eval.answer_collection import collect_answers, require_isolated_resources
from app.rag_eval.answer_dataset import DEFAULT_CASES, PROJECT_ROOT, json_digest, load_answer_cases, validate_answer_cases
from app.rag_eval.answer_quality import _build_backend, _revision, _write_json
from app.rag_eval.answer_scoring import METRICS, score_snapshots, validate_snapshot_rows
from app.shared.utils.statistics import percentile

MODES = ("legacy", "agentic")
BUDGET_FIELDS = ("max_tool_calls", "max_web_calls", "max_repairs", "max_context_tokens",
                 "timeout_seconds", "keyword_scan_limit", "neighbor_window")


def _text(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def validate_evaluation_snapshot(snapshot: dict) -> None:
    if not isinstance(snapshot, dict) or any(not _text(snapshot.get(k)) for k in
                                            ("snapshot_id", "corpus_version", "index_version")):
        raise ValueError("评测快照缺少 snapshot_id、corpus_version 或 index_version")
    faq = snapshot.get("approved_faq")
    if not isinstance(faq, dict) or type(faq.get("enabled")) is not bool:
        raise ValueError("评测快照必须声明 approved_faq.enabled")
    if faq["enabled"]:
        if (not _text(faq.get("collection")) or not faq["collection"].startswith("eval_")
                or not _text(faq.get("index_version"))
                or not re.fullmatch(r"[0-9a-f]{64}", str(faq.get("content_sha256", "")))
                or not re.fullmatch(r"[0-9a-f]{64}", str(faq.get("approval_sha256", "")))):
            raise ValueError("FAQ 快照缺少隔离集合、内容摘要或索引版本")


def faq_content_digest(rows: list[dict]) -> str:
    """对所有导出的 active 标量字段做哈希，并保持行序稳定。"""
    ids = [r.get("evo_doc_id") for r in rows]
    if any(not _text(key) for key in ids) or len(set(ids)) != len(ids):
        raise ValueError("FAQ 快照 ID 无效或重复")
    if any(r.get("status") != "active" for r in rows):
        raise ValueError("FAQ 快照只允许 active 条目")
    scalars = [{k: v for k, v in row.items() if k not in ("dense_vector", "sparse_vector")}
               for row in rows]
    return json_digest(sorted(scalars, key=lambda r: r["evo_doc_id"]))


def capture_faq_snapshot(settings, expected: dict | None = None) -> dict:
    """读取实际的评估 FAQ 内容。不创建集合或索引。"""
    resources = require_isolated_resources(settings)
    name = resources.get("evolution_collection")
    if not name:
        actual = {"enabled": False}
    else:
        from app.shared.clients.milvus_gateway import milvus_gateway
        client = milvus_gateway.milvus_client
        if client is None or not client.has_collection(collection_name=name, timeout=settings.milvus.timeout_seconds):
            raise ValueError("隔离 FAQ 集合不存在；不能把连接失败当作空快照")
        iterator = client.query_iterator(collection_name=name, filter="status == 'active'",
                                         output_fields=["*"], batch_size=500,
                                         timeout=settings.milvus.timeout_seconds)
        rows = []
        try:
            while batch := iterator.next():
                rows.extend(batch)
        finally:
            iterator.close()
        from app.evolution.governance import approved_faq_snapshot
        approved = approved_faq_snapshot()
        index_versions = sorted({str(r["index_version"]) for r in approved})
        actual = {"enabled": True, "collection": name, "content_sha256": faq_content_digest(rows),
                  "row_count": len(rows), "approval_sha256": json_digest(approved),
                  "index_versions": index_versions,
                  "index_version": index_versions[0] if len(index_versions) == 1 else "empty" if not approved else "mixed"}
    if expected is not None:
        for key in ("enabled", "collection", "content_sha256", "approval_sha256", "index_version"):
            if actual.get(key) != expected.get(key):
                raise ValueError(f"真实 FAQ 与评测快照不一致：{key}")
    return actual


def _prompt_digest(root: Path) -> str:
    files = {p.relative_to(root).as_posix(): p.read_text(encoding="utf-8")
             for p in sorted((root / "app/resources/prompts").rglob("*")) if p.is_file()}
    return json_digest(files)


def validate_pair(pair: dict, cases: list[dict], manifest: dict) -> None:
    if not isinstance(pair, dict) or pair.get("schema_version") != 1:
        raise ValueError("不支持的配对快照格式")
    shared = pair.get("shared")
    if not isinstance(shared, dict):
        raise ValueError("配对快照缺少共享清单")
    for key in ("dataset_sha256", "documents"):
        if shared.get(key) != manifest[key]:
            raise ValueError(f"配对快照与题库不一致：{key}")
    validate_evaluation_snapshot(shared.get("evaluation_snapshot"))
    for key in ("answer_model", "prompts_sha256", "code_sha256", "policy_version"):
        if not _text(shared.get(key)):
            raise ValueError(f"共享清单缺少版本：{key}")
    if not isinstance(shared.get("budgets"), dict) or not shared["budgets"]:
        raise ValueError("共享清单缺少预算")
    digest = json_digest(shared)
    if pair.get("shared_sha256") != digest:
        raise ValueError("共享清单内容摘要不一致")
    runs = pair.get("runs")
    if not isinstance(runs, dict) or set(runs) != set(MODES):
        raise ValueError("配对快照必须同时包含 legacy 和 agentic")
    for mode in MODES:
        run = runs[mode]
        if not isinstance(run, dict) or run.get("mode") != mode or run.get("shared_sha256") != digest:
            raise ValueError("运行模式或共享清单摘要不一致")
        validate_snapshot_rows(cases, run.get("results"))


def _check_scenarios(cases, snapshot):
    for case in cases:
        fixture = case.get("scenario_requirements")
        if fixture is not None and (fixture.get("status") != "verified"
                                    or fixture.get("snapshot_id") != snapshot["snapshot_id"]):
            raise ValueError(f"扩展题的测试夹具尚未验证或快照不一致：{case['case_id']}")


def _numbers(values):
    return [float(v) for v in values if type(v) in (int, float) and math.isfinite(v) and v >= 0]


def _measure(values, total):
    known = _numbers(values)
    return {"mean": mean(known) if known else None, "known_count": len(known),
            "coverage": len(known) / total, "reason": None if len(known) == total else "missing_measurements"}


def runtime_summary(rows: list[dict]) -> dict:
    total = len(rows)
    latency = _measure([r.get("elapsed_ms") for r in rows], total)
    known = _numbers([r.get("elapsed_ms") for r in rows])
    for label, p in (("p50", .5), ("p95", .95)):
        latency[label] = percentile(known, p)
    cost = _measure([(r.get("query_metrics") or {}).get("cost_usd") for r in rows], total)
    cost["total"] = cost["mean"] * total if cost["coverage"] == 1 else None
    counts = Counter(r.get("agentic_stop_reason") for r in rows if r.get("agentic_stop_reason"))
    failures = Counter(t.get("status") for r in rows for t in r.get("agentic_trace", [])
                       if t.get("status") not in (None, "ok", "empty"))
    return {"request_count": total, "latency_ms": latency, "cost_usd": cost,
            "completion_rate": sum(not r.get("error") and bool(r.get("response")) for r in rows) / total,
            "stop_reasons": dict(counts), "tool_failures": dict(failures),
            "first_token_ms": _measure([(r.get("query_metrics") or {}).get("first_token_ms") for r in rows], total),
            **{key: _measure([(r.get("query_metrics") or {}).get(key) for r in rows], total)
               for key in ("llm_calls", "input_tokens", "output_tokens")}}


def _paired_deltas(left: list[dict], right: list[dict]) -> dict:
    by_id = {r["case_id"]: r for r in right}
    result = {}
    for name in METRICS:
        deltas = [by_id[r["case_id"]]["metrics"][name]["value"] - r["metrics"][name]["value"]
                  for r in left if r["metrics"][name]["value"] is not None
                  and by_id[r["case_id"]]["metrics"][name]["value"] is not None]
        uncertainty = 1.96 * stdev(deltas) / math.sqrt(len(deltas)) if len(deltas) > 1 else None
        result[name] = {"mean_delta": mean(deltas) if deltas else None, "paired_count": len(deltas),
                        "case_count": len(left), "paired_coverage": len(deltas) / len(left),
                        "approximate_95pct_half_width": uncertainty,
                        "uncertainty_note": "normal approximation; repeated runs and reviewed cases still required"}
    return result


def score_pair(pair, cases, manifest, scorers, scoring_config, *, timeout_seconds=120) -> dict:
    validate_pair(pair, cases, manifest)
    runs = {}
    for mode in MODES:
        rows = pair["runs"][mode]["results"]
        report = score_snapshots(cases, rows, scorers, timeout_seconds=timeout_seconds)
        report["runtime"] = runtime_summary(rows)
        runs[mode] = report
    left, right = (runs[m]["results"] for m in MODES)
    categories = sorted({c["category"] for c in cases})
    complete = all(metric["coverage"] == 1 for r in runs.values() for metric in r["summary"]["metrics"].values())
    return {"schema_version": 1, "mode": "paired_comparison", "dataset": manifest,
            "shared": pair["shared"], "shared_sha256": pair["shared_sha256"],
            "scoring_config": scoring_config, "runs": runs, "comparison": _paired_deltas(left, right),
            "by_category": {category: _paired_deltas([r for r in left if r["category"] == category],
                                                      [r for r in right if r["category"] == category])
                            for category in categories},
            "ready_for_baseline": manifest["ready_for_baseline"] and complete,
            "baseline_note": "Draft or incomplete runs are exploratory. Paired deltas are not acceptance results."}


def _collect_pair(cases, manifest, evaluation_snapshot, project_root, *, standalone=False):
    if not standalone:
        raise ValueError("真实双模式采集仅支持独立 CLI 进程，禁止修改服务进程的模式")
    from app.shared.config import settings
    resources = require_isolated_resources(settings)
    expected = evaluation_snapshot["approved_faq"]
    capture_faq_snapshot(settings, expected)
    from app.shared.clients.mongo import close_mongo_client
    from app.rag.query.pipeline import invoke_query_graph
    agentic = settings.agentic
    shared = {"dataset_sha256": manifest["dataset_sha256"], "documents": manifest["documents"],
              "evaluation_snapshot": evaluation_snapshot, "answer_model": settings.llm.llm_model or "qwen3-32b",
              "answer_temperature": settings.llm.temperature, "decision_model": agentic.decision_model,
              "budgets": {key: getattr(agentic, key) for key in BUDGET_FIELDS},
              "prompts_sha256": _prompt_digest(project_root), "policy_version": agentic.policy_version,
              **_revision(project_root), "resources": resources}
    digest = json_digest(shared)
    pair = {"schema_version": 1, "shared": shared, "shared_sha256": digest, "runs": {}}
    original_mode, original_gray = agentic.mode, agentic.gray_percent
    try:
        for mode in MODES:
            agentic.mode, agentic.gray_percent = mode, 100
            capture_faq_snapshot(settings, expected)
            rows = collect_answers(cases, invoke_query_graph)
            capture_faq_snapshot(settings, expected)
            pair["runs"][mode] = {"mode": mode, "gray_percent": 100, "shared_sha256": digest, "results": rows}
    finally:
        agentic.mode, agentic.gray_percent = original_mode, original_gray
        close_mongo_client()
    return pair


def _check_paths(output, snapshot_output, inputs, documents, root):
    protected = {Path(p).resolve() for p in inputs if p is not None}
    protected.update((root / name).resolve() for name in documents)
    for path in (output, snapshot_output):
        if path.resolve() in protected or path.with_name(path.name + ".tmp").resolve() in protected:
            raise ValueError("输出不能覆盖题库、手册或输入快照")


def run_comparison(cases_path=DEFAULT_CASES, report_path="output/agentic-comparison.json", *,
                   evaluation_snapshot_path=None, snapshots_path=None, allow_external=False,
                   allow_unreviewed=False, project_root=PROJECT_ROOT, _standalone=False):
    cases = load_answer_cases(cases_path)
    manifest = validate_answer_cases(cases, project_root=project_root)
    pair = json.loads(Path(snapshots_path).read_text(encoding="utf-8")) if snapshots_path else None
    evaluation_snapshot = (json.loads(Path(evaluation_snapshot_path).read_text(encoding="utf-8"))
                           if evaluation_snapshot_path else None)
    if evaluation_snapshot is not None:
        validate_evaluation_snapshot(evaluation_snapshot)
    if pair is not None:
        validate_pair(pair, cases, manifest)
        if evaluation_snapshot is not None and pair["shared"]["evaluation_snapshot"] != evaluation_snapshot:
            raise ValueError("配对运行与指定评测快照不一致")
    if not allow_external:
        return {"mode": "validation_only", "dataset": manifest, "ready_for_baseline": False,
                "snapshots_validated": pair is not None,
                "evaluation_snapshot_validated": evaluation_snapshot is not None,
                "blocking_review_count": manifest["case_count"] - manifest["reviewed_count"]}
    if not manifest["ready_for_baseline"] and not allow_unreviewed:
        raise ValueError("真实评测前需要人工复核；--allow-unreviewed 只允许草稿调试")
    if pair is None and evaluation_snapshot is None:
        raise ValueError("真实采集必须指定 --eval-snapshot")
    _check_scenarios(cases, pair["shared"]["evaluation_snapshot"] if pair is not None else evaluation_snapshot)
    output = Path(report_path)
    snapshot_output = output.with_name(output.stem + ".snapshots.json")
    _check_paths(output, snapshot_output, (cases_path, snapshots_path, evaluation_snapshot_path),
                 manifest["documents"], project_root)
    from app.shared.config import settings
    from app.rag_eval.ragas_adapter import evaluation_config
    config, scoring_config = evaluation_config(settings)
    if pair is None:
        require_isolated_resources(settings)
        if not _standalone:
            raise ValueError("真实双模式采集仅支持独立 CLI 进程")
    scorers, client = _build_backend(config)
    try:
        if pair is None:
            pair = _collect_pair(cases, manifest, evaluation_snapshot, project_root, standalone=_standalone)
            validate_pair(pair, cases, manifest)
            _write_json(snapshot_output, pair)
        report = score_pair(pair, cases, manifest, scorers, scoring_config, timeout_seconds=config.timeout_seconds)
        report["evaluator"] = _revision(project_root)
        report["same_answer_and_judge_model"] = pair["shared"]["answer_model"] == config.model
        report["snapshots_path"] = str(Path(snapshots_path or snapshot_output).resolve())
        _write_json(output, report)
        return report
    finally:
        if client is not None:
            asyncio.run(client.close())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--output", type=Path, default=Path("output/agentic-comparison.json"))
    parser.add_argument("--eval-snapshot", type=Path)
    parser.add_argument("--snapshots", type=Path)
    parser.add_argument("--allow-external", action="store_true")
    parser.add_argument("--allow-unreviewed", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = run_comparison(args.cases, args.output, evaluation_snapshot_path=args.eval_snapshot,
                                snapshots_path=args.snapshots, allow_external=args.allow_external,
                                allow_unreviewed=args.allow_unreviewed, _standalone=True)
    except (ValueError, OSError, RuntimeError) as exc:
        parser.error(str(exc))
    print(json.dumps({k: v for k, v in result.items() if k not in ("runs", "by_category")},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
