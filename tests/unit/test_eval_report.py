"""报告对比：回归、提升、延迟方向与缺失指标。"""
from __future__ import annotations

from app.rag_eval.report import compare_reports, flatten_metrics, render_markdown


def _report(recall=0.8, ragas=0.9, latency=100.0) -> dict:
    return {
        "meta": {
            "created_at": "2026-01-01T00:00:00", "base_url": "http://x", "dataset": "d",
            "ragas_enabled": True, "ks": [5],
        },
        "summary": {
            "total": 10,
            "succeeded": 9,
            "failed": 1,
            "retrieval": {"reranked": {"recall@5": recall, "recall@5_n": 9, "must_hit@5": 1.0}},
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
                "retrieval": {"reranked": {"recall@5": recall, "must_hit@5": 1.0}},
                "ragas": {"faithfulness": ragas},
            },
            {
                "case_id": "bad", "status": "failed", "error": "模拟失败", "latency_ms": 0.0,
            },
        ],
    }


def test_flatten_metrics_skips_counts():
    flat = flatten_metrics(_report())
    assert flat["retrieval.reranked.recall@5"] == 0.8
    assert flat["ragas.faithfulness"] == 0.9
    assert all(not key.endswith("_n") for key in flat)


def test_compare_detects_regression_and_improvement():
    baseline = _report(recall=0.8, ragas=0.9, latency=100.0)
    current = _report(recall=0.7, ragas=0.95, latency=120.0)

    result = compare_reports(baseline, current, max_drop=0.02)

    assert [item["metric"] for item in result["regressions"]] == [
        "latency_ms.avg",
        "latency_ms.p95",
        "retrieval.reranked.recall@5",
    ]
    assert [item["metric"] for item in result["improvements"]] == ["ragas.faithfulness"]
    assert result["deltas"]["retrieval.reranked.recall@5"]["delta"] == -0.1


def test_compare_ignores_missing_metrics():
    baseline = _report()
    current = _report()
    current["summary"]["ragas"] = {}
    result = compare_reports(baseline, current, max_drop=0.02)
    assert "ragas.faithfulness" in result["only_in_baseline"]
    assert "ragas.faithfulness" not in result["deltas"]


def test_compare_no_change():
    result = compare_reports(_report(), _report(), max_drop=0.02)
    assert result["regressions"] == []
    assert result["improvements"] == []


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
    report["cases"][0]["retrieval"]["reranked"] = {"recall@5": None, "must_hit@5": None}
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
