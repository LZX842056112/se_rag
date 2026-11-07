"""LLM-as-judge 人工校准：固定 judge 与 rubric，计算一致率和 Cohen's kappa。

使用方式：
  python -m app.rag_eval.calibration --report answer-quality-report.json \\
    --labels human_labels.json --metric faithfulness --output calibration.json
"""
from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path
from statistics import mean


def _extract_rows(report: dict, mode: str | None):
    if mode:
        return report["runs"][mode]["results"]
    return report["results"]


def load_scores(report: dict, metric: str, mode: str | None = None) -> dict[str, float]:
    rows = _extract_rows(report, mode)
    scores = {}
    for row in rows:
        value = ((row.get("metrics") or {}).get(metric) or {}).get("value")
        if isinstance(value, (int, float)) and math.isfinite(value):
            scores[str(row["case_id"])] = float(value)
    return scores


def load_human_labels(path: Path | str) -> dict[str, float]:
    path = Path(path)
    text = path.read_text(encoding="utf-8-sig")
    if path.suffix.lower() == ".csv":
        result = {}
        for row in csv.DictReader(text.splitlines()):
            case_id = str(row.get("case_id") or "").strip()
            score = row.get("score")
            if case_id and score not in (None, ""):
                result[case_id] = float(score)
        return result
    data = json.loads(text)
    if isinstance(data, dict):
        return {str(key): float(value) for key, value in data.items()}
    return {str(row["case_id"]): float(row["score"]) for row in data}


def cohen_kappa(judge_pass: list[bool], human_pass: list[bool]) -> float:
    total = len(judge_pass)
    if total == 0:
        return 0.0
    observed = sum(1 for left, right in zip(judge_pass, human_pass) if left == right) / total
    judge_rate = sum(judge_pass) / total
    human_rate = sum(human_pass) / total
    expected = judge_rate * human_rate + (1 - judge_rate) * (1 - human_rate)
    return (observed - expected) / (1 - expected) if expected < 1 else 1.0


def evaluate_calibration(
    report: dict,
    human_scores: dict[str, float],
    *,
    metric: str,
    mode: str | None = None,
    tolerance: float = 0.1,
    pass_threshold: float = 0.7,
    min_samples: int = 30,
    min_agreement: float = 0.8,
    min_kappa: float = 0.6,
) -> dict:
    config = report.get("scoring_config") or {}
    blockers = []
    if not config.get("judge_model"):
        blockers.append("报告缺少固定 judge_model")
    if not config.get("rubric_version"):
        blockers.append("报告缺少 rubric_version")
    judge_scores = load_scores(report, metric, mode)
    shared = sorted(set(judge_scores) & set(human_scores))
    if len(shared) < min_samples:
        blockers.append(f"人工校准样本 {len(shared)} 少于 {min_samples}")
    if not shared:
        return {"pass": False, "blockers": blockers, "metric": metric, "metric_name": metric,
                "judge_model": config.get("judge_model"), "rubric_version": config.get("rubric_version"),
                "sample_count": 0}

    differences = [abs(judge_scores[case] - human_scores[case]) for case in shared]
    agreement = sum(1 for value in differences if value <= tolerance) / len(differences)
    judge_pass = [judge_scores[case] >= pass_threshold for case in shared]
    human_pass = [human_scores[case] >= pass_threshold for case in shared]
    kappa = cohen_kappa(judge_pass, human_pass)
    if agreement < min_agreement:
        blockers.append(f"一致率 {agreement:.3f} 低于 {min_agreement}")
    if kappa < min_kappa:
        blockers.append(f"Cohen's kappa {kappa:.3f} 低于 {min_kappa}")
    return {
        "pass": not blockers, "blockers": blockers, "metric": metric,
        "judge_model": config.get("judge_model"), "rubric_version": config.get("rubric_version"),
        "sample_count": len(shared), "tolerance": tolerance, "pass_threshold": pass_threshold,
        "mean_absolute_error": mean(differences), "agreement_within_tolerance": agreement,
        "cohen_kappa": kappa, "judge_pass_rate": sum(judge_pass) / len(shared),
        "human_pass_rate": sum(human_pass) / len(shared),
        "unmatched_judge_cases": sorted(set(judge_scores) - set(human_scores))[:10],
        "unmatched_human_cases": sorted(set(human_scores) - set(judge_scores))[:10],
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--labels", type=Path, required=True)
    parser.add_argument("--metric", default="faithfulness")
    parser.add_argument("--mode", choices=("legacy", "agentic"))
    parser.add_argument("--tolerance", type=float, default=0.1)
    parser.add_argument("--pass-threshold", type=float, default=0.7)
    parser.add_argument("--min-samples", type=int, default=30)
    parser.add_argument("--min-agreement", type=float, default=0.8)
    parser.add_argument("--min-kappa", type=float, default=0.6)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    report = json.loads(args.report.read_text(encoding="utf-8"))
    labels = load_human_labels(args.labels)
    result = evaluate_calibration(report, labels, metric=args.metric, mode=args.mode,
                                  tolerance=args.tolerance, pass_threshold=args.pass_threshold,
                                  min_samples=args.min_samples, min_agreement=args.min_agreement,
                                  min_kappa=args.min_kappa)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
