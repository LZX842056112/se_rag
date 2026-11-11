"""评测命令行入口。

用法：
    python -m app.rag_eval.cli run --dataset app/rag_eval/datasets/sample_cases.json
    python -m app.rag_eval.cli run --dataset ... --baseline output/eval/baseline.json --fail-on-regression
    python -m app.rag_eval.cli compare --baseline output/eval/baseline.json --current output/eval/latest.json
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.rag_eval.config import EvalConfig
from app.rag_eval.report import (
    compare_reports,
    load_report,
    render_markdown,
    save_baseline,
    write_report,
)
from app.rag_eval.runner import timed_run


def _parse_ks(value: str) -> tuple[int, ...]:
    items = [int(item.strip()) for item in value.split(",") if item.strip()]
    return tuple(sorted({item for item in items if item > 0})) or (5,)


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
    run.add_argument("--max-drop", default=0.02, type=float, help="回归阈值")
    run.add_argument("--save-baseline", type=Path, help="把本次结果保存为基线")
    run.add_argument("--fail-on-regression", action="store_true")

    compare = sub.add_parser("compare", help="对比两份报告")
    compare.add_argument("--baseline", required=True, type=Path)
    compare.add_argument("--current", required=True, type=Path)
    compare.add_argument("--max-drop", default=0.02, type=float)

    args = parser.parse_args(argv)
    if args.command == "compare":
        return _compare(args.baseline, args.current, args.max_drop)
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
    )
    try:
        report = timed_run(config, progress=print)
    except Exception as exc:  # noqa: BLE001 - CLI 统一转成可读错误与退出码
        print(f"评测失败：{exc}", file=sys.stderr)
        return 2

    comparison = None
    if args.baseline:
        # 先对比再落盘，保证 Markdown 报告中同样能看到基线对比结论
        comparison = compare_reports(load_report(args.baseline), report, config.max_drop)
        report["comparison"] = comparison

    path = write_report(report, config.output_dir)
    print(f"报告已写入：{path}")
    print(render_markdown(report))

    if args.save_baseline:
        save_baseline(report, args.save_baseline)
        print(f"基线已保存：{args.save_baseline}")

    if comparison is not None:
        _print_comparison(comparison)
        if args.fail_on_regression and comparison["regressions"]:
            return 1
    return 0


def _compare(baseline_path: Path, current_path: Path, max_drop: float) -> int:
    try:
        result = compare_reports(load_report(baseline_path), load_report(current_path), max_drop)
    except Exception as exc:  # noqa: BLE001
        print(f"对比失败：{exc}", file=sys.stderr)
        return 2
    _print_comparison(result)
    return 1 if result["regressions"] else 0


def _print_comparison(result: dict) -> None:
    print("")
    print(f"对比指标数：{result['compared_metrics']}，阈值：{result['max_drop']}")
    for item in result["regressions"]:
        print(f"回归  {item['metric']}: {item['baseline']} -> {item['current']} ({item['delta']:+.4f})")
    for item in result["improvements"]:
        print(f"提升  {item['metric']}: {item['baseline']} -> {item['current']} ({item['delta']:+.4f})")
    if not result["regressions"] and not result["improvements"]:
        print("没有超过阈值的指标变化")


if __name__ == "__main__":
    raise SystemExit(main())
