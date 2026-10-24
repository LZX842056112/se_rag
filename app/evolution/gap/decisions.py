"""复用人工忽略决定。只抑制同一上下文的自动派单，不修改失败信号。"""
from __future__ import annotations

import math

from app.evolution.issue_groups import ACTIVE_STATUSES, ISSUE_SCAN_LIMIT, context_key
from app.shared.config import settings
from app.shared.runtime.logger import logger


def ignored_contexts(rows: list[dict], get_collection=None) -> dict[tuple, dict]:
    requested = {key for row in rows if (key := context_key(row)) is not None}
    if not requested:
        return {}
    if get_collection is None:
        from app.evolution.repositories import get_collection
    try:
        docs = get_collection(settings.mongo.k_gaps_collection).find({
            "session_id": {"$in": list({key[0] for key in requested})},
            "status": "dismissed", "reviewed_at": {"$exists": True},
        }, {"query": 1, "session_id": 1, "item_names": 1, "business_tag": 1,
            "reviewed_at": 1, "review_reason": 1}).sort("reviewed_at", -1).limit(ISSUE_SCAN_LIMIT)
        result = {}
        for doc in docs:
            stamp = doc.get("reviewed_at")
            if isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or not math.isfinite(stamp) or stamp <= 0:
                continue
            if not str(doc.get("review_reason") or "").strip():
                continue
            key = context_key(doc)
            if key in requested and key not in result:
                result[key] = doc
        return result
    except Exception as exc:
        # 无法验证人工决定时，继续登记真实失败，不能自动当作已忽略。
        logger.warning(f"读取缺口忽略决定失败：{type(exc).__name__}")
        return {}


def ignored_gap(row: dict, get_collection=None) -> dict | None:
    return ignored_contexts([row], get_collection).get(context_key(row))


def apply_ignored_decisions(rows: list[dict], get_collection) -> list[dict]:
    """读取侧立即反映人工决定，原始登记和已完成历史保持原样。"""
    decisions = ignored_contexts([row for row in rows if row.get("status") in ACTIVE_STATUSES], get_collection)
    result = []
    for row in rows:
        decision = decisions.get(context_key(row)) if row.get("status") in ACTIVE_STATUSES else None
        if decision:
            row = {**row, "raw_status": row["status"], "status": "dismissed",
                   "dismissed_by_gap_id": str(decision["_id"]), "suppression_reason": decision["review_reason"],
                   "suppressed_at": decision["reviewed_at"]}
        result.append(row)
    return result
