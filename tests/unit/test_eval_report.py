"""报告对比：回归、提升、延迟方向与缺失指标。"""
from __future__ import annotations

import json

import pytest

from app.rag_eval.report import compare_reports, flatten_metrics, render_markdown


def _report(recall=0.8, ragas=0.9, latency=100.0) -> dict:
    return {
        "meta": {
            "created_at": "2026-01-01T00:00:00", "base_url": "http://x", "dataset": "d",
            "ragas_enabled": True, "ks": [5],
            "dataset_sha256": "a" * 64, "case_ids": ["bad", "ok"],
            "metric_schema_version": 3, "evaluation_schema_versions": [1],
        },
        "summary": {
            "total": 10,
            "succeeded": 9,
            "failed": 1,
            "retrieval": {"final": {"recall@5": recall, "recall@5_n": 9, "must_hit@5": 1.0}},
            "ragas": {"faithfulness": ragas, "faithfulness_n": 9},
            "latency_ms": {"avg": latency, "p95": latency * 2},
            "conclusion": {
                "status": "failed" if recall < 0.8 else "warning",
                "reasons": ["1 题执行失败"],
                "key_metrics": {"recall@5": recall, "must_hit@5": 1.0, "faithfulness": ragas},
            },
        },
        "cases": [
            {
                "case_id": "ok", "status": "succeeded", "answer": "a", "contexts": ["c"],
                "evidence_count": 1, "latency_ms": 12.0,
                "retrieval": {"final": {"recall@5": recall, "must_hit@5": 1.0}},
                "ragas": {"faithfulness": ragas},
            },
            {
                "case_id": "bad", "status": "failed", "error": "模拟失败", "latency_ms": 0.0,
            },
        ],
    }


def test_flatten_metrics_skips_counts():
    flat = flatten_metrics(_report())
    assert flat["retrieval.final.recall@5"] == 0.8
    assert flat["ragas.faithfulness"] == 0.9
    assert all(not key.endswith("_n") for key in flat)


def test_compare_detects_regression_and_improvement():
    baseline = _report(recall=0.8, ragas=0.9, latency=100.0)
    current = _report(recall=0.7, ragas=0.95, latency=120.0)

    result = compare_reports(baseline, current, max_drop=0.02, max_latency_increase_ms=10)

    assert result["comparable"] is True
    assert [item["metric"] for item in result["regressions"]] == [
        "latency_ms.avg",
        "latency_ms.p95",
        "retrieval.final.recall@5",
    ]
    assert [item["metric"] for item in result["improvements"]] == ["ragas.faithfulness"]
    assert result["deltas"]["retrieval.final.recall@5"]["delta"] == -0.1


def test_compare_ignores_missing_metrics():
    baseline = _report()
    current = _report()
    current["summary"]["ragas"] = {}
    result = compare_reports(baseline, current, max_drop=0.02)
    assert "ragas.faithfulness" in result["only_in_baseline"]
    assert "ragas.faithfulness" not in result["deltas"]
    assert "ragas.faithfulness" in result["coverage_regressions"]


def test_compare_uses_milliseconds_for_latency_and_reports_sample_count_drops():
    baseline = _report()
    current = _report(latency=120.0)
    current["summary"]["ragas"]["faithfulness_n"] = 8
    result = compare_reports(baseline, current)
    assert result["regressions"] == []
    assert result["coverage_regressions"] == ["ragas.faithfulness"]


def test_compare_warns_when_reports_use_different_inputs_or_schemas():
    baseline = _report()
    current = _report()
    baseline["meta"].update(metric_schema_version=1, dataset_sha256="aaa")
    current["meta"].update(metric_schema_version=2, dataset_sha256="bbb")
    warnings = compare_reports(baseline, current)["warnings"]
    assert any("metric_schema_version" in warning for warning in warnings)
    assert any("dataset_sha256" in warning for warning in warnings)


@pytest.mark.parametrize(("key", "value"), [
    ("dataset_sha256", "b" * 64),
    ("case_ids", ["ok"]),
    ("metric_schema_version", 2),
    ("evaluation_schema_versions", [2]),
    ("case_ids", None),
    ("evaluation_schema_versions", []),
])
def test_comparison_with_incompatible_inputs_only_reports_descriptive_deltas(key, value):
    baseline = _report()
    current = _report(recall=0.7, latency=4000.0)
    current["summary"]["ragas"] = {}
    current["meta"][key] = value

    result = compare_reports(baseline, current)

    assert result["comparable"] is False
    assert any(key in reason for reason in result["incompatibilities"])
    assert result["deltas"]["retrieval.final.recall@5"]["delta"] == -0.1
    assert result["regressions"] == result["improvements"] == result["coverage_regressions"] == []
    assert result["coverage_drops"] == {}
    current["comparison"] = result
    text = render_markdown(current)
    assert "两份报告不可比" in text
    assert "没有发现指标或覆盖率回归" not in text


@pytest.mark.parametrize(("comparable", "acceptance_status", "exit_code"), [
    (True, "passed", 1),
    (False, "passed", 0),
    (False, "failed", 1),
])
def test_compare_command_uses_comparability_for_regressions_but_keeps_acceptance(
    tmp_path, comparable, acceptance_status, exit_code,
):
    from app.rag_eval.cli import main

    baseline = _report()
    current = _report(recall=0.7)
    if not comparable:
        current["meta"]["dataset_sha256"] = "b" * 64
    current["summary"]["conclusion"].update(status=acceptance_status, valid_measurement=True)
    before = tmp_path / "baseline.json"
    after = tmp_path / "current.json"
    before.write_text(json.dumps(baseline), encoding="utf-8")
    after.write_text(json.dumps(current), encoding="utf-8")

    assert main(["compare", "--baseline", str(before), "--current", str(after)]) == exit_code


def test_compare_no_change():
    current = _report()
    current["meta"]["case_ids"].reverse()
    result = compare_reports(_report(), current, max_drop=0.02)
    assert result["comparable"] is True
    assert result["regressions"] == []
    assert result["improvements"] == []
    assert result["acceptance_failures"] == []


def test_render_markdown_contains_sections():
    text = render_markdown(_report())
    assert "# RAG 评测报告" in text
    assert "## 总体结论" in text
    assert "## 检索指标" in text
    assert "## RAGAS 生成指标" in text
    assert "recall@5" in text


def test_render_markdown_shows_case_status_and_errors():
    """逐题状态、关键指标与失败原因必须直接出现在报告里。"""
    text = render_markdown(_report())
    assert "## 逐题结果" in text
    assert "| ok | 成功 |" in text
    assert "| bad | 失败 |" in text
    assert "模拟失败" in text


def test_render_markdown_marks_unannotated_metrics():
    report = _report()
    report["cases"][0]["retrieval"]["final"] = {"recall@5": None, "must_hit@5": None}
    text = render_markdown(report)
    assert "无标注" in text


def test_render_markdown_includes_comparison_section():
    report = _report()
    report["comparison"] = {
        "compared_metrics": 3,
        "max_drop": 0.02,
        "regressions": [{"metric": "retrieval.reranked.recall@5", "baseline": 0.9, "current": 0.7, "delta": -0.2}],
        "improvements": [],
    }
    text = render_markdown(report)
    assert "## 基线对比" in text
    assert "发现 1 项回归" in text
    assert "retrieval.reranked.recall@5" in text


def test_render_markdown_identifies_zero_scores_for_manual_review():
    report = _report()
    report["cases"][0]["ragas"] = {"faithfulness": 0.0, "answer_relevancy": 0.9}
    report["cases"][1]["ragas"] = {"error": "连接失败", "faithfulness": None}

    markdown = render_markdown(report)

    assert "## RAGAS 零分复核" in markdown
    assert "- `faithfulness` 有 1 题为零分。用例 `ok`。" in markdown
    assert report["cases"][0]["ragas"]["faithfulness"] == 0.0
