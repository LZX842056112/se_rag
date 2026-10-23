"""离线答案质量评测。默认只校验；真实调用必须增加 --allow-external。"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import re
import subprocess

from app.rag_eval.answer_collection import collect_answers, require_isolated_resources
from app.rag_eval.answer_dataset import DEFAULT_CASES, PROJECT_ROOT, load_answer_cases, validate_answer_cases
from app.rag_eval.answer_scoring import compare_baseline, score_snapshots, validate_snapshot_rows


def validate_snapshots(snapshot: dict, cases: list[dict], manifest: dict) -> None:
    if not isinstance(snapshot, dict) or snapshot.get("schema_version") != 1:
        raise ValueError("不支持的答案快照格式")
    for key in ("dataset_sha256", "documents"):
        if snapshot.get(key) != manifest[key]:
            raise ValueError(f"快照与题库不一致：{key}")
    if not isinstance(snapshot.get("generation"), dict) or not snapshot["generation"].get("revision"):
        raise ValueError("快照缺少生成版本")
    generation = snapshot["generation"]
    faq = generation.get("approved_faq")
    resources = generation.get("resources") or {}
    if resources.get("evolution_collection") or (faq or {}).get("enabled"):
        if (not isinstance(faq, dict) or not faq.get("enabled") or not faq.get("index_version")
                or any(not re.fullmatch(r"[0-9a-f]{64}", str(faq.get(key, "")))
                       for key in ("content_sha256", "approval_sha256"))):
            raise ValueError("启用 FAQ 的快照必须保留真实 FAQ 内容和审批摘要及索引版本")
    validate_snapshot_rows(cases, snapshot.get("results"))


def _revision(root: Path) -> dict:
    def git(*args):
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                              text=True, timeout=5, check=False)
    try:
        head = git("rev-parse", "HEAD")
        revision = head.stdout.strip() if head.returncode == 0 else "unavailable"
        dirty = bool(git("status", "--porcelain").stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        revision, dirty = "unavailable", None
    digest = hashlib.sha256()
    for path in sorted([*root.glob("app/**/*.py"), *root.glob("app/resources/prompts/*")]):
        if path.is_file():
            digest.update(path.relative_to(root).as_posix().encode())
            digest.update(path.read_bytes())
    return {"revision": revision, "dirty": dirty, "code_sha256": digest.hexdigest()}


def _collect_real(cases):
    from app.shared.config import settings
    resources = require_isolated_resources(settings)
    from app.shared.clients.mongo import close_mongo_client
    from app.rag.query.pipeline import invoke_query_graph
    try:
        if "evolution_collection" in resources:
            from app.rag_eval.agentic_comparison import capture_faq_snapshot
            resources["approved_faq"] = capture_faq_snapshot(settings)
        rows = collect_answers(cases, invoke_query_graph)
        if "approved_faq" in resources and capture_faq_snapshot(settings) != resources["approved_faq"]:
            raise ValueError("评测期间已审批 FAQ 内容发生变化")
        return rows, resources
    finally:
        close_mongo_client()


def _build_backend(config):
    from app.rag_eval.ragas_adapter import create_ragas_scorers
    return create_ragas_scorers(config)


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def _check_output_paths(output, snapshot_output, *, cases_path, snapshots_path, baseline_path,
                        manifest, project_root):
    protected = {Path(cases_path).resolve()}
    protected.update((project_root / name).resolve() for name in manifest["documents"])
    protected.update(Path(p).resolve() for p in (snapshots_path, baseline_path) if p is not None)
    destinations = [output]
    if snapshots_path is None:
        destinations.append(snapshot_output)
    for path in destinations:
        if path.resolve() in protected or path.with_name(path.name + ".tmp").resolve() in protected:
            raise ValueError("输出不能覆盖题库、来源手册、答案快照或基线文件")


def run_answer_eval(cases_path: Path | str = DEFAULT_CASES,
                    report_path: Path | str = "output/answer-quality-report.json", *,
                    allow_external: bool = False, snapshots_path: Path | str | None = None,
                    baseline_path: Path | str | None = None, allow_unreviewed: bool = False,
                    limit: int | None = None, project_root: Path = PROJECT_ROOT) -> dict:
    """校验或运行一批任务；快照允许在无需再次查询的情况下打分。"""
    cases = load_answer_cases(cases_path)
    if limit is not None:
        if type(limit) is not int or limit < 1:
            raise ValueError("limit 必须是正整数")
        cases = cases[:limit]
    manifest = validate_answer_cases(cases, project_root=project_root)
    snapshot = None
    if snapshots_path is not None:
        snapshot = json.loads(Path(snapshots_path).read_text(encoding="utf-8"))
        validate_snapshots(snapshot, cases, manifest)
    if not allow_external:
        return {"mode": "validation_only", "dataset": manifest,
                "snapshot_validated": snapshot is not None}
    if not manifest["ready_for_baseline"] and not allow_unreviewed:
        raise ValueError("真实评测前请人工复核题库；调试草稿可用 --allow-unreviewed，但不能作为可信基线")
    from app.shared.config import settings
    from app.rag_eval.ragas_adapter import evaluation_config
    config, scoring_config = evaluation_config(settings)
    output = Path(report_path)
    snapshot_output = output.with_name(output.stem + ".snapshots.json")
    _check_output_paths(output, snapshot_output, cases_path=cases_path, snapshots_path=snapshots_path,
                        baseline_path=baseline_path, manifest=manifest, project_root=project_root)
    baseline = json.loads(Path(baseline_path).read_text(encoding="utf-8")) if baseline_path else None
    if baseline is not None:
        for key, value in {"dataset_sha256": manifest["dataset_sha256"], "documents": manifest["documents"],
                           "scoring_config": scoring_config}.items():
            if baseline.get(key) != value or not baseline.get("ready_for_baseline"):
                raise ValueError(f"基线不兼容或未经复核：{key}")
    if snapshot is None:
        require_isolated_resources(settings)
    scorers, client = _build_backend(config)
    try:
        if snapshot is None:
            rows, resources = _collect_real(cases)
            generation = {**_revision(project_root), "answer_model": settings.llm.llm_model or "qwen3-32b",
                          "resources": resources}
            if "approved_faq" in resources:
                generation["approved_faq"] = resources["approved_faq"]
            snapshot = {"schema_version": 1, "dataset_sha256": manifest["dataset_sha256"],
                        "documents": manifest["documents"], "generation": generation, "results": rows}
            _write_json(snapshot_output, snapshot)
    except BaseException:
        if client is not None:
            asyncio.run(client.close())
        raise
    report = score_snapshots(cases, snapshot["results"], scorers, timeout_seconds=config.timeout_seconds,
                             close=client.close if client is not None else None)
    report.update(schema_version=1, mode="answer_quality", dataset=manifest,
                  dataset_sha256=manifest["dataset_sha256"], documents=manifest["documents"],
                  generation=snapshot["generation"], evaluator=_revision(project_root), scoring_config=scoring_config,
                  approved_faq=snapshot["generation"].get("approved_faq"),
                  same_answer_and_judge_model=snapshot["generation"].get("answer_model") == config.model,
                  snapshots_path=str(Path(snapshots_path or snapshot_output).resolve()),
                  ready_for_baseline=manifest["ready_for_baseline"] and all(
                      m["coverage"] == 1 for m in report["summary"]["metrics"].values()))
    if baseline is not None:
        if report["ready_for_baseline"]:
            report["comparison"] = compare_baseline(report, baseline)
        else:
            report["comparison_error"] = "incomplete_scoring"
    _write_json(output, report)
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--output", type=Path, default=Path("output/answer-quality-report.json"))
    parser.add_argument("--allow-external", action="store_true")
    parser.add_argument("--allow-unreviewed", action="store_true")
    parser.add_argument("--snapshots", type=Path)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--limit", type=int)
    args = parser.parse_args(argv)
    try:
        result = run_answer_eval(args.cases, args.output, allow_external=args.allow_external,
                                 allow_unreviewed=args.allow_unreviewed, snapshots_path=args.snapshots,
                                 baseline_path=args.baseline, limit=args.limit)
    except (ValueError, OSError, RuntimeError) as exc:
        parser.error(str(exc))
    print(json.dumps(result.get("summary", result), ensure_ascii=False, indent=2))
    if args.allow_external:
        print(str(args.output.resolve()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())