"""评测集审计：规模、复核、标准答案与出处覆盖。

这个模块只读文件，不调用模型或数据库。默认门槛按 100-300 条已复核样本检查。
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from app.rag_eval.answer_dataset import DEFAULT_CASES, PROJECT_ROOT, load_answer_cases, validate_answer_cases


def _normalized_query(query: str) -> str:
    return re.sub(r"\s+", "", str(query or "").lower())


def audit_answer_dataset(
    cases_path: Path | str = DEFAULT_CASES,
    *,
    min_cases: int = 100,
    max_cases: int = 300,
    require_reviewed: bool = True,
    project_root: Path = PROJECT_ROOT,
) -> dict:
    """审计答案评测集；返回 ``pass``、``blockers``、``stats``。"""
    if min_cases < 0 or max_cases < min_cases:
        raise ValueError("评测集规模区间不合法")
    cases = load_answer_cases(cases_path)
    manifest = validate_answer_cases(cases, project_root=project_root)
    blockers: list[str] = []

    if len(cases) < min_cases:
        blockers.append(f"样本数 {len(cases)} 少于下限 {min_cases}")
    if len(cases) > max_cases:
        blockers.append(f"样本数 {len(cases)} 多于上限 {max_cases}")
    if require_reviewed and manifest["reviewed_count"] < len(cases):
        blockers.append(f"已复核 {manifest['reviewed_count']}/{len(cases)}，未达到全部复核")
    if manifest["reference_count"] < len(cases):
        blockers.append(f"标准答案 {manifest['reference_count']}/{len(cases)}，未全覆盖")
    if not manifest["documents"]:
        blockers.append("没有可校验的来源文档")

    queries = [_normalized_query(case["query"]) for case in cases]
    duplicates = sorted({query for query in queries if queries.count(query) > 1})
    if duplicates:
        blockers.append(f"重复问题 {len(duplicates)} 条，例如 {duplicates[0][:40]}")

    review_queue = [case["case_id"] for case in cases if case.get("annotation_status") != "reviewed"]
    stats = {
        "case_count": len(cases),
        "reviewed_count": manifest["reviewed_count"],
        "reference_count": manifest["reference_count"],
        "review_coverage": round(manifest["reviewed_count"] / len(cases), 4) if cases else 0.0,
        "reference_coverage": round(manifest["reference_count"] / len(cases), 4) if cases else 0.0,
        "unique_queries": len(set(queries)),
        "documents": len(manifest["documents"]),
        "products": manifest["products"],
        "categories": manifest["categories"],
        "review_queue": review_queue,
    }
    return {"pass": not blockers, "blockers": blockers, "stats": stats,
            "dataset": manifest, "dataset_sha256": manifest["dataset_sha256"]}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--min", type=int, default=100)
    parser.add_argument("--max", type=int, default=300)
    parser.add_argument("--require-reviewed", action="store_true")
    parser.add_argument("--warn-only", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--review-queue", type=Path)
    args = parser.parse_args(argv)
    try:
        result = audit_answer_dataset(args.cases, min_cases=args.min, max_cases=args.max,
                                      require_reviewed=args.require_reviewed)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.review_queue:
        args.review_queue.parent.mkdir(parents=True, exist_ok=True)
        args.review_queue.write_text(json.dumps(result["stats"]["review_queue"], ensure_ascii=False, indent=2),
                                     encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["pass"] or args.warn_only:
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
