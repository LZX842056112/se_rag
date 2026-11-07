"""评测回归：单测 + 数据集审计 + 基线对比。

本地运行：
  python scripts/run_eval_regression.py
  python scripts/run_eval_regression.py --baseline output/baseline.json --current output/current.json
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--current", type=Path)
    parser.add_argument("--thresholds", type=Path)
    parser.add_argument("--strict-dataset", action="store_true")
    parser.add_argument("--skip-pytest", action="store_true")
    parser.add_argument("--allow-draft", action="store_true")
    args = parser.parse_args()

    failures = []
    if not args.skip_pytest:
        code = subprocess.call([sys.executable, "-m", "pytest", "-q"], cwd=ROOT)
        if code != 0:
            failures.append(f"pytest 失败，退出码 {code}")

    from app.rag_eval.dataset_audit import audit_answer_dataset
    dataset = audit_answer_dataset(min_cases=100, max_cases=300, require_reviewed=True)
    print("dataset audit:", json.dumps({key: dataset[key] for key in ("pass", "blockers")},
                                       ensure_ascii=False))
    if args.strict_dataset and not dataset["pass"]:
        failures.append("数据集未达到 100-300 条已复核门槛")

    if args.baseline and args.current:
        from app.rag_eval.regression import compare_reports
        current = json.loads(args.current.read_text(encoding="utf-8"))
        baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
        thresholds = (json.loads(args.thresholds.read_text(encoding="utf-8"))
                      if args.thresholds else None)
        result = compare_reports(current, baseline, thresholds=thresholds, allow_draft=args.allow_draft)
        print("regression:", json.dumps(result, ensure_ascii=False, indent=2))
        if not result["pass"]:
            failures.append("评测指标回归超过阈值")

    print(json.dumps({"pass": not failures, "failures": failures}, ensure_ascii=False, indent=2))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
