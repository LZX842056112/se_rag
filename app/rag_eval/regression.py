"""离线评测回归门禁：比较当前报告与基线，超过阈值即失败。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

DEFAULT_THRESHOLDS = {
    "faithfulness": -0.02,
    "answer_relevancy": -0.03,
    "context_precision": -0.03,
    "context_recall": -0.03,
    "factual_correctness": -0.03,
}
DEFAULT_COVERAGE_DROP = 0.05


def compare_reports(
    current: dict,
    baseline: dict,
    *,
    thresholds: dict[str, float] | None = None,
    coverage_drop: float = DEFAULT_COVERAGE_DROP,
    allow_draft: bool = False,
) -> dict:
    """比较两份报告；返回 ``pass``、``deltas``、``failures``。"""
    thresholds = thresholds or DEFAULT_THRESHOLDS
    failures: list[str] = []
    for key in ("dataset_sha256", "documents", "scoring_config"):
        if current.get(key) != baseline.get(key):
            failures.append(f"基线不兼容：{key} 不一致")
    if not allow_draft:
        if not current.get("ready_for_baseline"):
            failures.append("当前报告不是可信基线（ready_for_baseline=false）")
        if not baseline.get("ready_for_baseline"):
            failures.append("基线报告不是可信基线（ready_for_baseline=false）")

    deltas = {}
    for name, previous in (baseline.get("summary", {}).get("metrics") or {}).items():
        current_metric = (current.get("summary", {}).get("metrics") or {}).get(name) or {}
        current_mean = current_metric.get("mean")
        previous_mean = previous.get("mean")
        if current_mean is None or previous_mean is None:
            deltas[name] = {"mean_delta": None, "coverage_delta": None}
            failures.append(f"{name} 缺少可比较的均值")
            continue
        delta = current_mean - previous_mean
        coverage_delta = current_metric.get("coverage", 0.0) - previous.get("coverage", 0.0)
        deltas[name] = {"mean_delta": delta, "coverage_delta": coverage_delta}
        threshold = thresholds.get(name, -0.03)
        if delta < threshold:
            failures.append(f"{name} 下降 {delta:.3f}，低于阈值 {threshold}")
        if coverage_delta < -coverage_drop:
            failures.append(f"{name} 覆盖率下降 {coverage_delta:.3f}")
    return {"pass": not failures, "failures": failures, "deltas": deltas,
            "thresholds": thresholds, "coverage_drop": coverage_drop}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--current", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--thresholds-json", type=Path)
    parser.add_argument("--allow-draft", action="store_true")
    args = parser.parse_args(argv)
    current = json.loads(args.current.read_text(encoding="utf-8"))
    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    thresholds = (json.loads(args.thresholds_json.read_text(encoding="utf-8"))
                  if args.thresholds_json else None)
    result = compare_reports(current, baseline, thresholds=thresholds, allow_draft=args.allow_draft)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
