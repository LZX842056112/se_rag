"""评测命令行入口。

用法：
    python -m app.rag_eval.cli run --dataset app/rag_eval/datasets/sample_cases.json
    python -m app.rag_eval.cli run --dataset ... --baseline output/eval/baseline.json --fail-on-regression
    python -m app.rag_eval.cli compare --baseline output/eval/baseline.json --current output/eval/latest.json
"""
from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

from app.rag_eval.config import DEFAULT_MAX_DROP, DEFAULT_MAX_LATENCY_INCREASE_MS, EvalConfig
from app.rag_eval.report import (
    compare_reports,
    load_report,
    render_comparison,
    render_markdown,
    save_baseline,
    write_report,
)
from app.rag_eval.runner import timed_run


def _parse_ks(value: str) -> tuple[int, ...]:
    items = [int(item.strip()) for item in value.split(",") if item.strip()]
    return tuple(sorted({item for item in items if item > 0})) or (5,)


def _non_negative(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise argparse.ArgumentTypeError("阈值必须是大于或等于零的有限数值")
    return number


def _comparison_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--max-drop", default=DEFAULT_MAX_DROP, type=_non_negative, help="质量分数下降阈值")
    parser.add_argument(
        "--max-latency-increase-ms", default=DEFAULT_MAX_LATENCY_INCREASE_MS,
        type=_non_negative, help="延迟增加阈值，单位毫秒",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.rag_eval.cli", description="RAG 评测")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="执行评测")
    run.add_argument("--dataset", required=True, type=Path)
    run.add_argument("--output-dir", default=Path("output/eval"), type=Path)
    run.add_argument("--base-url", default="http://127.0.0.1:8001")
    run.add_argument("--concurrency", default=2, type=int)
    run.add_argument("--timeout", default=180.0, type=float)
    run.add_argument("--retries", default=1, type=int)
    run.add_argument("--ks", default="3,5,10", type=_parse_ks)
    run.add_argument("--tags", default="", help="只跑指定标签，逗号分隔")
    run.add_argument("--no-ragas", action="store_true", help="跳过 RAGAS 生成指标")
    run.add_argument("--baseline", type=Path, help="运行后与该基线对比")
    _comparison_options(run)
    run.add_argument("--save-baseline", type=Path, help="把本次结果保存为基线")
    run.add_argument("--fail-on-regression", action="store_true")

    compare = sub.add_parser("compare", help="对比两份报告")
    compare.add_argument("--baseline", required=True, type=Path)
    compare.add_argument("--current", required=True, type=Path)
    _comparison_options(compare)

    args = parser.parse_args(argv)
    if args.command == "compare":
        return _compare(args)
    return _run(args)


def _run(args) -> int:
    tags = tuple(item.strip() for item in args.tags.split(",") if item.strip())
    config = EvalConfig(
        dataset_path=args.dataset,
        output_dir=args.output_dir,
        base_url=args.base_url,
        ks=args.ks,
        concurrency=max(1, args.concurrency),
        timeout_seconds=max(1.0, args.timeout),
        retries=max(0, args.retries),
        run_ragas=not args.no_ragas,
        max_drop=args.max_drop,
        tags=tags,
        max_latency_increase_ms=args.max_latency_increase_ms,
    )
    comparison = None
    try:
        report = timed_run(config, progress=lambda message: print(message, flush=True))
        if args.baseline:
            comparison = compare_reports(
                load_report(args.baseline), report, config.max_drop, config.max_latency_increase_ms,
            )
            report["comparison"] = comparison
        path = write_report(report, config.output_dir)
        conclusion = report["summary"]["conclusion"]
        if args.save_baseline:
            if conclusion.get("valid_measurement", False):
                save_baseline(report, args.save_baseline)
                print(f"基线已保存：{args.save_baseline}")
            else:
                print("基线未保存。请修复执行、行为或分数覆盖率问题。", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 - CLI 统一转成可读错误与退出码
        print(f"评测失败：{exc}", file=sys.stderr)
        return 2

    print(f"报告已写入：{path}")
    print(render_markdown(report))
    if conclusion["status"] in ("failed", "warning"):
        return 1
    if args.save_baseline and not conclusion.get("valid_measurement", False):
        return 1
    if args.fail_on_regression and comparison and _has_regression(comparison):
        return 1
    return 0


def _compare(args) -> int:
    try:
        result = compare_reports(
            load_report(args.baseline), load_report(args.current), args.max_drop, args.max_latency_increase_ms,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"对比失败：{exc}", file=sys.stderr)
        return 2
    print("\n".join(render_comparison(result)))
    return int(_has_regression(result) or bool(result["acceptance_failures"]))


def _has_regression(result: dict) -> bool:
    return result["comparable"] and bool(result["regressions"] or result["coverage_regressions"])


if __name__ == "__main__":
    raise SystemExit(main())
