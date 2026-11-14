"""评测报告：JSON / Markdown 落盘，以及基线对比。

报告结构见 ``runner._build_report``。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from app.rag_eval.config import DEFAULT_MAX_DROP, DEFAULT_MAX_LATENCY_INCREASE_MS
from app.rag_eval.ragas_metrics import METRIC_NAMES, finite_score
from app.rag_eval.retrieval_metrics import LAYERS

# 延迟是「越小越好」，其余指标是「越大越好」
_LOWER_IS_BETTER = {"latency_ms.avg", "latency_ms.p95"}
_SERVER_PROVENANCE_FIELDS = (
    "service_commit", "service_dirty", "service_source_sha256", "knowledge_base_version", "worker_count",
    "runtime_backend", "models",
)


def write_report(report: dict, output_dir: Path) -> Path:
    """写入带时间戳的报告，并刷新 ``latest.json``。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = output_dir / f"eval_{stamp}.json"
    text = json.dumps(report, ensure_ascii=False, indent=2)
    path.write_text(text, encoding="utf-8")
    (output_dir / "latest.json").write_text(text, encoding="utf-8")
    (output_dir / f"eval_{stamp}.md").write_text(render_markdown(report), encoding="utf-8")
    return path


def load_report(path: Path) -> dict:
    """读取报告 JSON。"""
    return json.loads(path.read_text(encoding="utf-8"))


def save_baseline(report: dict, path: Path) -> None:
    """把一次报告保存为基线。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def flatten_metrics(report: dict) -> dict[str, float]:
    """把嵌套汇总拍平成 ``layer.metric@k`` / ``ragas.metric`` 形式。"""
    return {
        f"{prefix}.{name}": float(value)
        for prefix, metrics in _metric_groups(report)
        for name, value in metrics.items()
        if not name.endswith("_n") and finite_score(value)
    }


def _metric_groups(report: dict):
    summary = report.get("summary") or {}
    for layer, metrics in (summary.get("retrieval") or {}).items():
        yield f"retrieval.{layer}", metrics
    for name in ("ragas", "latency_ms"):
        yield name, summary.get(name) or {}


def _metric_counts(report: dict) -> dict[str, int]:
    counts = {
        f"{prefix}.{name[:-2]}": value
        for prefix, metrics in _metric_groups(report)
        for name, value in metrics.items()
        if name.endswith("_n") and type(value) is int
    }
    succeeded = (report.get("summary") or {}).get("succeeded")
    if type(succeeded) is int:
        counts.update({name: succeeded for name in _LOWER_IS_BETTER})
    return counts


def _comparison_incompatibilities(before: dict, after: dict) -> list[str]:
    reasons = []
    for key, value_type in (
        ("dataset_sha256", str), ("case_ids", list), ("metric_schema_version", int),
        ("evaluation_schema_versions", list),
    ):
        old, new = before.get(key), after.get(key)
        if not old or not new or type(old) is not value_type or type(new) is not value_type:
            reasons.append(f"{key} 缺失或无效，请用当前评测格式重新生成报告")
            continue
        if value_type is list:
            item_type = str if key == "case_ids" else int
            if any(type(item) is not item_type or not item for item in old + new):
                reasons.append(f"{key} 含无效值，请用当前评测格式重新生成报告")
                continue
            old, new = set(old), set(new)
        if old != new:
            reasons.append(f"{key} 不同")
    return reasons


def _server_provenance(meta: dict) -> dict:
    server = meta.get("server") or {}
    if not server.get("service_source_sha256") and meta.get("service_source_sha256"):
        return server | {"service_source_sha256": meta["service_source_sha256"]}
    return server


def _comparison_warnings(baseline: dict, current: dict) -> list[str]:
    before, after = baseline.get("meta") or {}, current.get("meta") or {}
    meta_keys = ("tags", "ks", "concurrency", "retries", "timeout_seconds", "ragas_enabled", "judge")
    warnings = [f"{key} 不同，请检查运行条件" for key in meta_keys if before.get(key) != after.get(key)]
    before_server, after_server = _server_provenance(before), _server_provenance(after)
    for key in _SERVER_PROVENANCE_FIELDS:
        if before_server.get(key) != after_server.get(key):
            warnings.append(f"服务 {key} 不同，请检查可比性")
    unknown = [
        key for key in _SERVER_PROVENANCE_FIELDS
        if before_server.get(key) is None or after_server.get(key) is None
    ]
    if unknown:
        warnings.append(f"服务来源信息不全，缺少 {', '.join(unknown)}")
    if any(server.get("service_dirty") and not server.get("service_source_sha256")
           for server in (before_server, after_server)):
        warnings.append("服务含未提交的改动。请补充运行代码的 SHA-256。")
    before_modes = (baseline.get("summary") or {}).get("query_modes")
    after_modes = (current.get("summary") or {}).get("query_modes")
    if before_modes != after_modes:
        warnings.append("实际查询路径分布不同，请检查分层样本是否可比")
    return warnings


def compare_reports(
    baseline: dict, current: dict, max_drop: float = DEFAULT_MAX_DROP,
    max_latency_increase_ms: float = DEFAULT_MAX_LATENCY_INCREASE_MS,
) -> dict:
    """对比两份报告，返回指标差值、回归项与改善项。"""
    incompatibilities = _comparison_incompatibilities(baseline.get("meta") or {}, current.get("meta") or {})
    comparable = not incompatibilities
    before = flatten_metrics(baseline)
    after = flatten_metrics(current)
    common = sorted(set(before) & set(after))

    deltas: dict[str, dict] = {}
    regressions: list[dict] = []
    improvements: list[dict] = []
    for key in common:
        delta = round(after[key] - before[key], 4)
        deltas[key] = {"baseline": before[key], "current": after[key], "delta": delta}
        if not comparable:
            continue
        is_latency = key in _LOWER_IS_BETTER
        direction = delta if is_latency else -delta
        threshold = max_latency_increase_ms if is_latency else max_drop
        entry = {"metric": key, "baseline": before[key], "current": after[key], "delta": delta}
        if direction > threshold:
            regressions.append(entry)
        elif direction < -threshold:
            improvements.append(entry)

    before_counts, after_counts = _metric_counts(baseline), _metric_counts(current)
    missing = set(before) - set(after)
    count_drops = {
        key for key, count in before_counts.items()
        if key in before and count > after_counts.get(key, 0)
    } if comparable else set()
    conclusion = (current.get("summary") or {}).get("conclusion") or {}
    current_failed = (
        "valid_measurement" in conclusion and conclusion.get("status") in ("failed", "warning")
    )
    return {
        "comparable": comparable,
        "incompatibilities": incompatibilities,
        "compared_metrics": len(common),
        "max_drop": max_drop,
        "max_latency_increase_ms": max_latency_increase_ms,
        "deltas": deltas,
        "regressions": regressions,
        "improvements": improvements,
        "coverage_regressions": sorted(missing | count_drops) if comparable else [],
        "coverage_drops": {
            name: {"baseline": before_counts[name], "current": after_counts.get(name, 0)}
            for name in sorted(count_drops)
        },
        "warnings": incompatibilities + _comparison_warnings(baseline, current),
        "acceptance_failures": conclusion.get("reasons", []) if current_failed else [],
        "only_in_baseline": sorted(missing),
        "only_in_current": sorted(set(after) - set(before)),
    }


def render_markdown(report: dict) -> str:
    """生成便于人工查看的 Markdown 摘要。"""
    meta = report.get("meta") or {}
    summary = report.get("summary") or {}
    lines = [
        "# RAG 评测报告",
        "",
        f"- 时间：{meta.get('created_at', '')}",
        f"- 服务：{meta.get('base_url', '')}",
        f"- 数据集：{meta.get('dataset', '')}",
        f"- 样本：{summary.get('total', 0)} 题，成功 {summary.get('succeeded', 0)}，失败 {summary.get('failed', 0)}",
        f"- RAGAS：{'开启' if meta.get('ragas_enabled') else '关闭'}",
    ]
    judge = meta.get("judge") or {}
    if judge.get("judge_model"):
        lines.append(f"- judge 模型：`{judge['judge_model']}`（ragas {judge.get('ragas_version', '')}）")
    if meta.get("metric_schema_version") is not None:
        lines.extend([
            f"- 数据集 SHA-256 `{meta.get('dataset_sha256', '')}`",
            f"- 指标版本 {meta['metric_schema_version']}，响应评测版本 {meta.get('evaluation_schema_versions')}",
            f"- 检索 k {meta.get('ks')}，筛选标签 {meta.get('tags')}，并发 {meta.get('concurrency')}",
        ])
        server = _server_provenance(meta)
        for key in _SERVER_PROVENANCE_FIELDS:
            value = server.get(key)
            lines.append(f"- {key} `{'未知' if value is None else value}`")
        lines.append(f"- 实际查询路径 {summary.get('query_modes', {})}")
        lines.append(f"- 实际结果类型 {summary.get('outcomes', {})}")
    lines.append("")

    conclusion = summary.get("conclusion") or {}
    if conclusion:
        status_cn = {
            "passed": "通过",
            "warning": "需关注",
            "failed": "未通过",
            "inconclusive": "无法判定",
        }.get(conclusion.get("status"), conclusion.get("status") or "未知")
        lines.extend(["## 总体结论", "", f"- 结论：**{status_cn}**"])
        for reason in conclusion.get("reasons") or []:
            lines.append(f"- 依据：{reason}")
        key_metrics = {
            key: value for key, value in (conclusion.get("key_metrics") or {}).items() if value is not None
        }
        if key_metrics:
            lines.append("- 关键指标：" + "，".join(f"{key}={value}" for key, value in key_metrics.items()))
        lines.append("")

    coverage = summary.get("coverage") or {}
    if coverage:
        lines.extend(["## 检查覆盖率", ""])
        for key, label in (("http", "HTTP"), ("behavior", "行为"), ("answer_context", "回答与上下文")):
            counts = coverage.get(key, {})
            lines.append(f"- {label} {counts.get('completed', 0)}/{counts.get('required', 0)}")
        for section, label in (("retrieval", "最终证据"), ("ragas", "RAGAS")):
            for name, counts in (coverage.get(section) or {}).items():
                lines.append(f"- {label} {name} {counts['completed']}/{counts['required']}")
        lines.append(f"- 评分状态 {summary.get('scoring_statuses', {})}")
        lines.append("")

    attempts = summary.get("http_attempts")
    degradation = summary.get("degradation")
    if attempts or degradation:
        lines.extend(["## HTTP 请求和服务降级", ""])
        if attempts:
            lines.append(f"- HTTP 请求共 {attempts['total']} 次。{attempts['retried_cases']} 题发生重试。")
            for reason, count in sorted(attempts["failed_attempts_by_reason"].items()):
                lines.append(f"  - `{reason}` {count} 次")
        if degradation:
            lines.append(f"- 服务提供了 {degradation['reported']} 题的降级标记。其中 {degradation['degraded']} 题降级。")
            for reason, count in degradation["by_reason"].items():
                lines.append(f"  - `{reason}` {count} 题")
        lines.append("")

    lines.extend(["## 检索指标", ""])
    has_retrieval = False
    for layer in LAYERS:
        metrics = (summary.get("retrieval") or {}).get(layer) or {}
        if not metrics:
            continue
        has_retrieval = True
        lines.append(f"### {layer}")
        lines.append("")
        lines.append("| 指标 | 值 | 样本数 |")
        lines.append("|---|---|---|")
        for name, value in metrics.items():
            if name.endswith("_n"):
                continue
            lines.append(f"| {name} | {value} | {metrics.get(f'{name}_n', '')} |")
        lines.append("")
    if not has_retrieval:
        lines.extend(["- 缺少可计算的检索分数。请查看切片标注和最终证据覆盖率。", ""])

    ragas = summary.get("ragas") or {}
    if ragas:
        lines.extend(["## RAGAS 生成指标", "", "| 指标 | 值 | 样本数 |", "|---|---|---|"])
        for name, value in ragas.items():
            if name.endswith("_n") or not finite_score(value):
                continue
            lines.append(f"| {name} | {value} | {ragas.get(f'{name}_n', '')} |")
        lines.append("")
    for name, count in (summary.get("ragas_retries") or {}).items():
        lines.append(f"- RAGAS `{name}` 因输出达到 token 上限重试 {count} 次。")
    if summary.get("ragas_retries"):
        lines.append("")

    zero_scores = {metric: [] for metric in METRIC_NAMES}
    for row in report.get("cases") or []:
        scores = row.get("ragas") or {}
        for metric in zero_scores:
            value = scores.get(metric)
            if finite_score(value) and value == 0:
                zero_scores[metric].append(row["case_id"])
    if any(zero_scores.values()):
        lines.extend(["## RAGAS 零分复核", "", "对照答案、参考答案和实际评分证据。保留评分器的原始分数。", ""])
        for metric, case_ids in zero_scores.items():
            if case_ids:
                ids = "、".join(f"`{case_id}`" for case_id in case_ids)
                lines.append(f"- `{metric}` 有 {len(case_ids)} 题为零分。用例 {ids}。")
        lines.append("")

    latency = summary.get("latency_ms") or {}
    lines.extend([
        "## 延迟",
        "",
        f"- 平均：{latency.get('avg')} ms",
        f"- P95：{latency.get('p95')} ms",
        "",
    ])
    server_timing = summary.get("server_timing_ms") or {}
    if server_timing:
        lines.extend(["### 服务端计时", "", "| 字段 | 平均 (ms) | P95 (ms) | 样本数 |", "|---|---|---|---|"])
        for name, values in server_timing.items():
            lines.append(f"| {name} | {values.get('avg')} | {values.get('p95')} | {values.get('n')} |")
        lines.append("")

    lines.extend(_render_case_table(report))
    lines.extend(render_comparison(report.get("comparison")))

    return "\n".join(lines)


def _render_case_table(report: dict) -> list[str]:
    """逐题结果表：状态、关键指标与失败原因都直接展示。"""
    cases = report.get("cases") or []
    if not cases:
        return []
    ks = (report.get("meta") or {}).get("ks") or [5]
    k = 5 if 5 in ks else max(ks)
    legacy = not (report.get("meta") or {}).get("metric_schema_version")
    layer = "reranked" if legacy else "final"

    lines = [
        "## 逐题结果",
        "",
        f"| 用例 | HTTP | 路径与结果 | 证据/上下文数 | {layer} recall@{k} | must_hit@{k} | faithfulness | 延迟(ms) | 说明 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for row in cases:
        failed = row.get("status") == "failed" or bool(row.get("error"))
        scores = (row.get("retrieval") or {}).get(layer) or {}
        ragas = row.get("ragas") or {}
        faithfulness = ragas.get("faithfulness")
        reasons = (row.get("behavior") or {}).get("reasons") or []
        notes = [row.get("error"), row.get("scoring_status"), *reasons, ragas.get("error")]
        if row.get("http_attempts", 0) > 1:
            notes.append(f"HTTP 尝试 {row['http_attempts']} 次")
        if row.get("http_failure_reasons"):
            notes.append("HTTP 失败原因 " + "、".join(row["http_failure_reasons"]))
        metrics = row.get("server_metrics") or {}
        if metrics.get("degraded") is True:
            reasons = metrics.get("degraded_reasons")
            notes.append("服务降级" + (f" {', '.join(map(str, reasons))}" if isinstance(reasons, list) else ""))
        for retry in row.get("ragas_retries", ()):
            status = "成功" if retry["status"] == "scored" else "失败"
            notes.append(f"{retry['metric']} 以 {retry['max_tokens']} token 重试{status}")
        note = "。".join(str(item) for item in notes if item)
        evidence = str(row.get("evidence_count", "-"))
        if "context_count" in row:
            evidence += f"/{row['context_count']}"
        lines.append(
            "| {case} | {status} | {route} | {evidence} | {recall} | {must_hit} | {faith} | {latency} | {note} |".format(
                case=row.get("case_id", ""),
                status="失败" if failed else "成功",
                route=f"{row.get('query_mode') or '-'}/{row.get('outcome') or '-'}",
                evidence=evidence,
                recall=_show_metric(scores, f"recall@{k}"),
                must_hit=_show_metric(scores, f"must_hit@{k}"),
                faith=faithfulness if finite_score(faithfulness) else "-",
                latency=row.get("latency_ms", "-"),
                note=" ".join(note.replace("|", "/").splitlines()),
            )
        )
    lines.append("")
    return lines


def _show_metric(scores: dict, name: str) -> str:
    if name not in scores:
        return "-"
    value = scores[name]
    return "无标注" if value is None else str(value)


def render_comparison(comparison: dict | None) -> list[str]:
    """基线对比结果直接写进报告，而不是只留在命令行输出。"""
    if not comparison:
        return []
    lines = ["## 基线对比", ""]
    regressions = comparison.get("regressions") or []
    improvements = comparison.get("improvements") or []
    coverage_regressions = comparison.get("coverage_regressions") or []
    acceptance = comparison.get("acceptance_failures") or []
    warnings = comparison.get("warnings") or []
    issues = len(regressions) + len(coverage_regressions)
    if comparison.get("comparable") is False:
        lines.append("- 两份报告不可比。")
    elif issues:
        lines.append(f"- 发现 {issues} 项回归")
    elif warnings:
        lines.append("- 对比有可比性警告。请先核对运行条件。")
    else:
        lines.append("- 没有发现指标或覆盖率回归")
    lines.append(f"- 对比指标数 {comparison.get('compared_metrics', 0)}，质量阈值 {comparison.get('max_drop')}")
    if comparison.get("max_latency_increase_ms") is not None:
        lines.append(f"- 延迟阈值 {comparison['max_latency_increase_ms']} ms")
    for warning in warnings:
        lines.append(f"- 警告 {warning}")
    drops = comparison.get("coverage_drops") or {}
    for metric in coverage_regressions:
        counts = drops.get(metric)
        detail = f"样本数从 {counts['baseline']} 降到 {counts['current']}" if counts else "缺少指标"
        lines.append(f"- 覆盖率回归 `{metric}`，{detail}")
    for reason in acceptance:
        lines.append(f"- 当前报告未达到验收条件。{reason}")
    lines.append("")
    if regressions or improvements:
        lines.extend(["| 类型 | 指标 | 基线 | 当前 | 变化 |", "|---|---|---|---|---|"])
        for item in regressions:
            lines.append(f"| 回归 | {item['metric']} | {item['baseline']} | {item['current']} | {item['delta']:+.4f} |")
        for item in improvements:
            lines.append(f"| 提升 | {item['metric']} | {item['baseline']} | {item['current']} | {item['delta']:+.4f} |")
        lines.append("")
    return lines
