"""离线样本与线上流量的 case_id 对齐检查。

线上回放同一道离线题时，请求必须携带 ``case_id``；``query_runs`` 与反馈事件会保存它。
本模块只做对齐覆盖检查，不把 Ragas 分数与线上解决率强行换算。
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from app.rag_eval.answer_dataset import DEFAULT_CASES, load_answer_cases


def load_online_rows(path: Path | str) -> list[dict]:
    text = Path(path).read_text(encoding="utf-8-sig")
    stripped = text.strip()
    if stripped.startswith("["):
        return json.loads(stripped)
    return [json.loads(line) for line in stripped.splitlines() if line.strip()]


def validate_alignment(offline_cases: list[dict], online_rows: list[dict]) -> dict:
    offline_ids = {str(case["case_id"]) for case in offline_cases}
    online_ids = []
    missing_case_id = 0
    for row in online_rows:
        case_id = str(row.get("case_id") or "").strip()
        if not case_id:
            missing_case_id += 1
            continue
        online_ids.append(case_id)
    duplicates = sorted({case_id for case_id in online_ids if online_ids.count(case_id) > 1})
    matched = sorted(set(online_ids) & offline_ids)
    unmatched_online = sorted(set(online_ids) - offline_ids)
    missing_online = sorted(offline_ids - set(online_ids))
    blockers = []
    if missing_case_id:
        blockers.append(f"{missing_case_id} 条线上记录缺少 case_id")
    if unmatched_online:
        blockers.append(f"{len(unmatched_online)} 个线上 case_id 不在离线题库")
    if missing_online:
        blockers.append(f"{len(missing_online)} 个离线样本没有线上回放")
    if duplicates:
        blockers.append(f"{len(duplicates)} 个 case_id 在线上出现多次")
    return {
        "pass": not blockers, "blockers": blockers,
        "offline_case_count": len(offline_ids), "online_row_count": len(online_rows),
        "matched_count": len(matched), "coverage": round(len(matched) / len(offline_ids), 4) if offline_ids else 0.0,
        "unmatched_online_case_ids": unmatched_online[:20],
        "missing_online_case_ids": missing_online[:20],
        "duplicate_online_case_ids": duplicates[:20],
    }


def mongo_rows(hours: float = 24.0) -> list[dict]:
    from app.shared.clients.mongo import get_collection
    from app.shared.config import settings
    since = time.time() - hours * 3600
    rows = list(get_collection(settings.mongo.query_runs_collection).find(
        {"ts": {"$gte": since}}, {"case_id": 1, "answer_id": 1, "intent": 1, "handoff": 1},
    ).limit(10000))
    return [{**row, "_id": str(row.get("_id", ""))} for row in rows]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--online", type=Path)
    parser.add_argument("--mongo-hours", type=float)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    cases = load_answer_cases(args.cases)
    if args.online:
        rows = load_online_rows(args.online)
    elif args.mongo_hours:
        rows = mongo_rows(args.mongo_hours)
    else:
        parser.error("必须提供 --online 或 --mongo-hours")
    result = validate_alignment(cases, rows)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
