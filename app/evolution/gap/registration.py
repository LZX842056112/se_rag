"""缺口领取与跨集合登记。未成功绑定缺口的暂存记录不进入审批。"""
from __future__ import annotations

import time
from contextlib import contextmanager
from contextvars import ContextVar

from bson import ObjectId

from app.evolution.repositories import evolution_repo
from app.shared.config import settings
from app.shared.runtime.logger import logger

_CLAIM: ContextVar[dict | None] = ContextVar("gap_registration_claim", default=None)
VISIBLE_REGISTRATION = {"$or": [{"registration_status": {"$exists": False}}, {"registration_status": "ready"}]}


class GapClaimLost(RuntimeError):
    """缺口已由其他任务接管，当前任务必须停止写入。"""


def claim_filter(gap: dict) -> dict:
    match = {"_id": gap["_id"], "status": "reviewing"}
    if gap.get("processing_owner"):
        match.update(status="processing", processing_owner=gap["processing_owner"], lease_until={"$gt": time.time()})
    return match


def require_current_claim() -> None:
    gap = _CLAIM.get()
    if gap and not evolution_repo.k_gaps.find_one(claim_filter(gap), {"_id": 1}):
        raise GapClaimLost("缺口领取已失效")


@contextmanager
def gap_claim_scope(gap: dict):
    token = _CLAIM.set(gap)
    try:
        require_current_claim()
        yield
    finally:
        _CLAIM.reset(token)


def registration_fields(gap_id: str | None) -> dict:
    require_current_claim()
    claim = _CLAIM.get()
    if not claim:
        return {}
    if gap_id != str(claim["_id"]):
        raise ValueError("登记记录与领取的缺口不匹配")
    return {"registration_status": "staged", "registration_owner": claim.get("processing_owner") or "manual"}


def _collections():
    from app.evolution import issues
    return ((evolution_repo.k_candidates, "candidate_id"),
            (issues.get_collection(settings.mongo.query_issues_collection), "issue_id"))


def activate_gap_registrations(gap: dict) -> None:
    """以已提交的缺口绑定为准。租约持有者不能自行使暂存记录可见。"""
    if gap.get("status") in {"candidate", "pending", "processing", "reviewing", "dismissed"}:
        return
    for collection, link in _collections():
        if gap.get(link):
            collection.update_one({"_id": ObjectId(gap[link]), "gap_id": str(gap["_id"]),
                                   "registration_status": "staged"},
                                  {"$set": {"registration_status": "ready"}})


def reconcile_registrations(limit: int) -> None:
    """恢复缺口绑定后中断的登记。轮转检查，避免孤立暂存记录阻塞恢复。"""
    for collection, link in _collections():
        rows = collection.find({"registration_status": "staged"}).sort("registration_checked_at", 1).limit(max(1, limit))
        for row in rows:
            try:
                collection.update_one({"_id": row["_id"]}, {"$set": {"registration_checked_at": time.time()}})
                gap = evolution_repo.k_gaps.find_one({"_id": ObjectId(row["gap_id"]), link: str(row["_id"])})
                if gap:
                    activate_gap_registrations(gap)
            except Exception as exc:
                logger.warning(f"恢复缺口登记失败：{type(exc).__name__}")
