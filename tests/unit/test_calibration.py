"""LLM-as-judge 人工校准。"""
import json
import pytest


def report(values, judge="judge-v1", rubric="rubric-v1"):
    return {
        "scoring_config": {"judge_model": judge, "rubric_version": rubric},
        "results": [
            {"case_id": f"case-{index}", "metrics": {"faithfulness": {"value": value}}}
            for index, value in enumerate(values)
        ],
    }


def test_calibration_passes_when_judge_matches_human(tmp_path):
    from app.rag_eval.calibration import evaluate_calibration
    labels = {f"case-{index}": value for index, value in enumerate([0.8, 0.9, 0.7])}
    result = evaluate_calibration(report([0.8, 0.9, 0.7]), labels, metric="faithfulness",
                                  min_samples=3, min_agreement=0.8, min_kappa=0.6)
    assert result["pass"] is True
    assert result["cohen_kappa"] == 1.0
    assert result["agreement_within_tolerance"] == 1.0


def test_calibration_blocks_missing_rubric_or_small_sample(tmp_path):
    from app.rag_eval.calibration import evaluate_calibration
    labels = {"case-0": 0.8}
    result = evaluate_calibration(report([0.8], rubric=""), labels, metric="faithfulness",
                                  min_samples=3)
    assert result["pass"] is False
    assert any("rubric_version" in item for item in result["blockers"])
    assert any("样本" in item for item in result["blockers"])


def test_calibration_detects_judge_bias(tmp_path):
    from app.rag_eval.calibration import evaluate_calibration
    labels = {f"case-{index}": 0.8 for index in range(5)}
    result = evaluate_calibration(report([0.2] * 5), labels, metric="faithfulness",
                                  min_samples=5, tolerance=0.1, pass_threshold=0.7)
    assert result["pass"] is False
    assert result["mean_absolute_error"] == pytest.approx(0.6)
    assert result["cohen_kappa"] == 0.0
