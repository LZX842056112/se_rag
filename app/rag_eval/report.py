"""评测报告：JSON / Markdown 落盘，以及基线对比。

报告结构见 ``runner._build_report``。对比只使用同名指标；某一侧缺失时跳过，
避免把「没跑这项」误判成回归。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from app.rag_eval.retrieval_metrics import LAYERS

# 延迟是「越小越好」，其余指标是「越大越好」
_LOWER_IS_BETTER = {"latency_ms.avg", "latency_ms.p95"}


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
    summary = report.get("summary") or {}
    flat: dict[str, float] = {}

    for layer, metrics in (summary.get("retrieval") or {}).items():
        for name, value in metrics.items():
            if name.endswith("_n") or value is None:
                continue
            flat[f"retrieval.{layer}.{name}"] = float(value)

    for name, value in (summary.get("ragas") or {}).items():
        if name.endswith("_n") or not isinstance(value, (int, float)):
            continue
        flat[f"ragas.{name}"] = float(value)

    for name, value in (summary.get("latency_ms") or {}).items():
        if isinstance(value, (int, float)):
            flat[f"latency_ms.{name}"] = float(value)
    return flat


def compare_reports(baseline: dict, current: dict, max_drop: float = 0.02) -> dict:
    """对比两份报告，返回指标差值、回归项与改善项。"""
    before = flatten_metrics(baseline)
    after = flatten_metrics(current)
    common = sorted(set(before) & set(after))

    deltas: dict[str, dict] = {}
    regressions: list[dict] = []
    improvements: list[dict] = []
    for key in common:
        delta = round(after[key] - before[key], 4)
        deltas[key] = {"baseline": before[key], "current": after[key], "delta": delta}
        worsened = delta < -max_drop if key not in _LOWER_IS_BETTER else delta > max_drop
        improved = delta > max_drop if key not in _LOWER_IS_BETTER else delta < -max_drop
        entry = {"metric": key, "baseline": before[key], "current": after[key], "delta": delta}
        if worsened:
            regressions.append(entry)
        elif improved:
            improvements.append(entry)

    return {
        "compared_metrics": len(common),
        "max_drop": max_drop,
        "deltas": deltas,
        "regressions": regressions,
        "improvements": improvements,
        "only_in_baseline": sorted(set(before) - set(after)),
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
        lines.extend(["- 本次没有可计算的检索指标（样本未标注 `gold_chunk_ids`）。", ""])

    ragas = summary.get("ragas") or {}
    if ragas:
        lines.extend(["## RAGAS 生成指标", "", "| 指标 | 值 | 样本数 |", "|---|---|---|"])
        for name, value in ragas.items():
            if name.endswith("_n") or not isinstance(value, (int, float)):
                continue
            lines.append(f"| {name} | {value} | {ragas.get(f'{name}_n', '')} |")
        lines.append("")

    latency = summary.get("latency_ms") or {}
    lines.extend([
        "## 延迟",
        "",
        f"- 平均：{latency.get('avg')} ms",
        f"- P95：{latency.get('p95')} ms",
        "",
    ])

    lines.extend(_render_case_table(report))
    lines.extend(_render_comparison(report.get("comparison")))

    return "\n".join(lines)


def _render_case_table(report: dict) -> list[str]:
    """逐题结果表：状态、关键指标与失败原因都直接展示。"""
    cases = report.get("cases") or []
    if not cases:
        return []
    ks = (report.get("meta") or {}).get("ks") or [5]
    k = 5 if 5 in ks else max(ks)

    lines = [
        "## 逐题结果",
        "",
        f"| 用例 | 状态 | 证据数 | recall@{k} | must_hit@{k} | faithfulness | 延迟(ms) | 说明 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for row in cases:
        failed = bool(row.get("error"))
        reranked = (row.get("retrieval") or {}).get("reranked") or {}
        ragas = row.get("ragas") or {}
        faithfulness = ragas.get("faithfulness")
        note = row.get("error") or ragas.get("error") or ""
        lines.append(
            "| {case} | {status} | {evidence} | {recall} | {must_hit} | {faith} | {latency} | {note} |".format(
                case=row.get("case_id", ""),
                status="失败" if failed else "成功",
                evidence=row.get("evidence_count", "-") if not failed else "-",
                recall=_show_metric(reranked.get(f"recall@{k}")) if not failed else "-",
                must_hit=_show_metric(reranked.get(f"must_hit@{k}")) if not failed else "-",
                faith=faithfulness if isinstance(faithfulness, (int, float)) else "-",
                latency=row.get("latency_ms", "-"),
                note=str(note).replace("|", "/"),
            )
        )
    lines.append("")
    return lines


def _show_metric(value: float | None) -> str:
    """指标为空表示该题没有标注，显式写「无标注」而不是 None。"""
    return "无标注" if value is None else str(value)


def _render_comparison(comparison: dict | None) -> list[str]:
    """基线对比结果直接写进报告，而不是只留在命令行输出。"""
    if not comparison:
        return []
    lines = ["## 基线对比", ""]
    regressions = comparison.get("regressions") or []
    improvements = comparison.get("improvements") or []
    lines.append(f"- 结论：{'发现 %d 项回归' % len(regressions) if regressions else '没有回归'}")
    lines.append(f"- 对比指标数：{comparison.get('compared_metrics', 0)}，阈值：{comparison.get('max_drop')}")
    lines.append("")
    if regressions or improvements:
        lines.extend(["| 类型 | 指标 | 基线 | 当前 | 变化 |", "|---|---|---|---|---|"])
        for item in regressions:
            lines.append(f"| 回归 | {item['metric']} | {item['baseline']} | {item['current']} | {item['delta']:+.4f} |")
        for item in improvements:
            lines.append(f"| 提升 | {item['metric']} | {item['baseline']} | {item['current']} | {item['delta']:+.4f} |")
        lines.append("")
    return lines
