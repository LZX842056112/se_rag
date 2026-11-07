"""评测回归门禁。"""
import copy


def report(value=0.8, coverage=1.0, ready=True):
    return {
        "dataset_sha256": "d", "documents": {"manual": "hash"},
        "scoring_config": {"judge_model": "judge-v1"},
        "ready_for_baseline": ready,
        "summary": {"metrics": {"faithfulness": {"mean": value, "coverage": coverage}}},
    }


def test_regression_blocks_metric_drop_and_dataset_mismatch():
    from app.rag_eval.regression import compare_reports
    baseline = report(0.8)
    current = report(0.75)
    result = compare_reports(current, baseline)
    assert result["pass"] is False
    assert any("faithfulness" in item for item in result["failures"])

    current = report(0.77)
    result = compare_reports(current, baseline, thresholds={"faithfulness": -0.02})
    assert result["pass"] is False
    result = compare_reports(report(0.8), baseline)
    assert result["pass"] is True

    changed = copy.deepcopy(current)
    changed["dataset_sha256"] = "other"
    result = compare_reports(changed, baseline)
    assert any("dataset_sha256" in item for item in result["failures"])


def test_regression_blocks_draft_reports_and_coverage_drop():
    from app.rag_eval.regression import compare_reports
    baseline = report(0.8, 1.0, ready=True)
    result = compare_reports(report(0.8, 1.0, ready=False), baseline)
    assert any("不是可信基线" in item for item in result["failures"])
    result = compare_reports(report(0.8, 0.9), baseline, coverage_drop=0.05)
    assert any("覆盖率下降" in item for item in result["failures"])
