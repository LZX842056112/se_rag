"""未解决问题待办。知识候选审批继续使用原有流程。"""
from __future__ import annotations

import hashlib
import time

from bson import ObjectId

from app.shared.clients.mongo import get_collection
from app.shared.config import settings
from app.shared.runtime.logger import logger
from app.shared.utils.answer import is_no_answer


def open_issue(state: dict) -> str:
    key = state.get("answer_id") or state.get("turn_id")
    if not key:
        key = hashlib.sha256((state.get("session_id", "") + state.get("original_query", "")).encode()).hexdigest()
    now = time.time()
    try:
        collection = get_collection(settings.mongo.query_issues_collection)
        # _id 使用固定键，原生唯一约束保证重复调用不会产生两个待办。
        existing = collection.find_one({"key": key})
        if existing:
            return str(existing["_id"])
        oid = ObjectId(hashlib.sha256(key.encode()).hexdigest()[:24])
        collection.update_one({"_id": oid}, {"$setOnInsert": {
            "key": key, "session_id": state.get("session_id", ""),
            "answer_id": state.get("answer_id", ""), "query": state.get("original_query", ""),
            "business_tag": state.get("business_tag", ""),
            "issue_type": state.get("issue_type") or "missing_material",
            "owner": settings.runtime.default_issue_owner, "status": "open",
            "resolution": "", "created_at": now, "updated_at": now, "completed_at": None,
        }}, upsert=True)
        return str(oid)
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
    query = {"status": status} if status else {}
    docs = get_collection(settings.mongo.query_issues_collection).find(query).sort("created_at", -1).limit(max(1, min(limit, 100)))
    return [{**{k: v for k, v in doc.items() if k != "_id"}, "id": str(doc["_id"])} for doc in docs]


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
        result = get_collection(settings.mongo.query_issues_collection).update_one(
            {"_id": ObjectId(issue_id)}, {"$set": {"owner": owner, "status": status,
            "resolution": resolution, "updated_at": now, "completed_at": now if status == "resolved" else None}},
        )
        return (True, "") if result.matched_count else (False, "待办不存在")
    except Exception as exc:
        logger.warning(f"更新待办失败：{exc}")
        return False, "待办更新失败"
