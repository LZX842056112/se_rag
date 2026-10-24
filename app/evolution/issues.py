"""未解决问题待办。知识候选审批继续使用原有流程。"""
from __future__ import annotations

import hashlib
import time

from bson import ObjectId

from app.evolution.gap.registration import GapClaimLost, VISIBLE_REGISTRATION, registration_fields, require_current_claim
from app.evolution.gap.decisions import apply_ignored_decisions, ignored_gap
from app.evolution.issue_groups import ACTIVE_STATUSES, ISSUE_SCAN_LIMIT, group_key, group_rows, public_group, with_context
from app.rag.query.business import classify_business
from app.shared.clients.mongo import get_collection
from app.shared.config import settings
from app.shared.runtime.logger import logger
from app.shared.utils.answer import is_no_answer


def open_issue(state: dict) -> str:
    fields = registration_fields(state.get("gap_id"))
    decision = ignored_gap(state, get_collection)
    if decision:
        logger.info(f"同题已人工忽略，不重复登记待办：gap_id={decision['_id']}")
        return ""
    key = state.get("answer_id") or state.get("turn_id")
    if not key:
        key = hashlib.sha256((state.get("session_id", "") + state.get("original_query", "")).encode()).hexdigest()
    now = time.time()
    try:
        collection = get_collection(settings.mongo.query_issues_collection)
        # _id 使用固定键，原生唯一约束保证重复调用不会产生两个待办。
        existing = collection.find_one({"key": key})
        if existing:
            require_current_claim()
            return str(existing["_id"])
        oid = ObjectId(hashlib.sha256(key.encode()).hexdigest()[:24])
        collection.update_one({"_id": oid}, {"$setOnInsert": {
            **fields,
            "key": key, "session_id": state.get("session_id", ""),
            "answer_id": state.get("answer_id", ""), "query": state.get("original_query", ""),
            "business_tag": state.get("business_tag") or classify_business(state.get("original_query") or ""),
            "item_names": list(state.get("item_names") or []),
            "issue_type": state.get("issue_type") or "missing_material",
            "gap_id": state.get("gap_id", ""), "gap_kind": state.get("gap_kind", ""),
            "owner": settings.runtime.default_issue_owner, "status": "open",
            "resolution": "", "created_at": now, "updated_at": now, "completed_at": None,
        }}, upsert=True)
        require_current_claim()
        return str(oid)
    except GapClaimLost:
        raise
    except Exception as exc:
        logger.warning(f"登记待办失败：{exc}")
        return ""


def register_unresolved(state: dict) -> str:
    if state.get("intent") == "chitchat":
        return ""
    if state.get("handoff_required") or is_no_answer(state.get("answer")):
        return open_issue(state)
    return ""


def list_issues(status: str | None = None, limit: int = 100) -> list[dict]:
    state_filter = {"status": {"$in": ACTIVE_STATUSES}} if status in ["unresolved", *ACTIVE_STATUSES] else ({"status": status} if status else {})
    if status == "dismissed":
        state_filter = {"status": {"$in": [*ACTIVE_STATUSES, "dismissed"]}}
    query = {"$and": [VISIBLE_REGISTRATION, state_filter]}
    docs = list(get_collection(settings.mongo.query_issues_collection).find(query).sort("created_at", -1).limit(ISSUE_SCAN_LIMIT))
    rows = apply_ignored_decisions(with_context(docs, get_collection), get_collection)
    groups = [public_group(members) for members in group_rows(rows)]
    if status:
        allowed = ACTIVE_STATUSES if status == "unresolved" else [status]
        groups = [row for row in groups if row["status"] in allowed]
    groups.sort(key=lambda row: (row["last_seen_at"], row["id"]), reverse=True)
    return groups[:max(1, min(limit, 100))]


def update_issue(issue_id: str, *, owner: str, status: str, resolution: str) -> tuple[bool, str]:
    if status not in ("open", "in_progress", "resolved"):
        return False, "处理状态不合法"
    owner, resolution = owner.strip(), resolution.strip()
    if status != "open" and not owner:
        return False, "请先指定负责人"
    if status == "resolved" and not resolution:
        return False, "请填写处理结果"
    try:
        now = time.time()
        collection = get_collection(settings.mongo.query_issues_collection)
        current = collection.find_one({"$and": [{"_id": ObjectId(issue_id)}, VISIBLE_REGISTRATION]})
        if not current:
            return False, "待办不存在"
        if current.get("status") == "resolved":
            if status == "resolved" and current.get("owner") == owner and current.get("resolution") == resolution:
                return True, ""
            return False, "待办已完成，不能覆盖处理结果；请刷新查看，后续问题另行登记"
        phase = {"status": {"$in": ACTIVE_STATUSES}} if current.get("status") in ACTIVE_STATUSES else {"status": current.get("status")}
        query = {"$and": [VISIBLE_REGISTRATION, {"session_id": current.get("session_id", "")}, phase]}
        rows = list(collection.find(query).limit(ISSUE_SCAN_LIMIT + 1))
        if len(rows) > ISSUE_SCAN_LIMIT:
            return False, "同会话待办超过处理上限，请先整理历史记录"
        if not any(row["_id"] == current["_id"] for row in rows):
            rows.append(current)
        rows = with_context(rows, get_collection)
        target = next(row for row in rows if row["_id"] == current["_id"])
        if ignored_gap(target, get_collection):
            return False, "该问题已在缺口复核中忽略，请刷新查看"
        members = [row for row in rows if group_key(row) == group_key(target)]
        patch = {"owner": owner, "status": status, "resolution": resolution,
                 "updated_at": now, "completed_at": now if status == "resolved" else None}
        for row in members:
            previous = {key: row.get(key) for key in ("owner", "status", "resolution", "completed_at")}
            update = {"$set": patch, "$push": {"handling_history": {**previous, "changed_at": now}}}
            result = collection.update_one({"_id": row["_id"], "status": row["status"], "updated_at": row.get("updated_at")}, update)
            if not result.matched_count:
                return False, "部分待办状态已变化，请刷新后核对处理结果"
        return True, ""
    except Exception as exc:
        logger.warning(f"更新待办失败：{exc}")
        return False, "待办更新失败"
