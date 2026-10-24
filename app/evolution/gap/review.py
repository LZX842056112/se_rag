"""缺口展示和人工确认。知识补充只登记空答案，不自动发布知识。"""
from __future__ import annotations

import time

from bson import ObjectId
from pymongo import ReturnDocument

from app.evolution.flags import writes_enabled
from app.evolution.candidate.storage import store_candidate
from app.evolution.gap.registration import GapClaimLost, activate_gap_registrations, claim_filter, gap_claim_scope
from app.evolution.models import KnowledgeCandidate
from app.evolution.repositories import evolution_repo
from app.rag.query.greetings import is_plain_greeting
from app.shared.runtime.logger import logger


def list_gaps(limit: int = 100) -> list[dict]:
    fields = ("query", "item_names", "business_tag", "gap_kind", "status", "confidence", "ts",
              "processing_reason", "candidate_id", "issue_id", "review_reason")
    limit = max(1, min(limit, 100))
    active = ["candidate", "pending", "strategy_pending", "reviewing", "processing"]
    collection = evolution_repo.k_gaps
    projection = {key: 1 for key in fields}
    rows = list(collection.find({"status": {"$in": active}}, projection).sort("ts", 1).limit(limit))
    if len(rows) < limit:
        rows.extend(collection.find({"status": {"$nin": active}}, projection).sort("ts", -1).limit(limit - len(rows)))
    return [{"id": str(row["_id"]), **{key: row[key] for key in fields if key in row}} for row in rows]


def finish_gap_review(gap: dict) -> str:
    """幂等完成已持久化的人工确认。中断后可从 reviewing 状态恢复。"""
    oid = gap["_id"]
    gap_id = str(oid)
    candidate = KnowledgeCandidate(
        faq_question=gap.get("query", ""), faq_answer="", source_refs=[],
        item_names=gap.get("item_names") or [], status="need_info", gap_id=gap_id,
        reason="人工确认知识缺失；请补充已核实的事实答案。" + gap.get("review_reason", ""),
    )
    with gap_claim_scope(gap):
        stored = store_candidate(candidate)
    candidate_id = str(stored["_id"])
    result = evolution_repo.k_gaps.update_one(claim_filter(gap), {"$set": {
        "status": stored["status"], "candidate_id": candidate_id,
        "processing_reason": "已登记待补充候选；补齐已核实答案后再审批", "processed_at": time.time(),
    }, "$unset": {"processing_owner": "", "lease_until": "", "resume_status": "", "next_retry_at": ""}})
    current = evolution_repo.k_gaps.find_one({"_id": oid}) or {}
    if not result.matched_count and current.get("candidate_id") != candidate_id:
        raise GapClaimLost("缺口复核状态已变化")
    activate_gap_registrations(current)
    return candidate_id


def review_gap(gap_id: str, *, disposition: str, reason: str) -> tuple[bool, str]:
    if not writes_enabled():
        return False, "知识写入已关闭"
    if disposition not in {"knowledge_missing", "dismissed"} or not reason.strip():
        return False, "请选择处理方式并填写原因"
    try:
        oid = ObjectId(gap_id)
        current = evolution_repo.k_gaps.find_one({"_id": oid})
        if not current:
            return False, "缺口不存在"
        if disposition == "knowledge_missing" and is_plain_greeting(current.get("query", "")):
            return False, "纯寒暄不能登记为知识候选"
        if current.get("candidate_id"):
            return (True, current["candidate_id"]) if disposition == "knowledge_missing" else (False, "请在候选卡片处理已有候选")
        if current.get("status") == "reviewing" and disposition == "knowledge_missing":
            return True, finish_gap_review(current)
        status = "reviewing" if disposition == "knowledge_missing" else "dismissed"
        patch = {"status": status, "review_reason": reason.strip(), "reviewed_at": time.time(),
                 "processing_reason": "人工确认知识缺失，等待登记候选" if status == "reviewing" else reason.strip()}
        if status == "reviewing":
            patch["gap_kind"] = "knowledge_missing"
        reviewed = evolution_repo.k_gaps.find_one_and_update(
            {"_id": oid, "status": {"$in": ["candidate", "strategy_pending", "pending"]}},
            {"$set": patch}, return_document=ReturnDocument.AFTER,
        )
        if not reviewed:
            # 另一请求可能已经完成相同确认。
            current = evolution_repo.k_gaps.find_one({"_id": oid}) or {}
            if disposition == "knowledge_missing" and current.get("candidate_id"):
                return True, current["candidate_id"]
            if disposition == "knowledge_missing" and current.get("status") == "reviewing":
                return True, finish_gap_review(current)
            return False, "缺口状态已变化，请刷新后重试"
        return (True, finish_gap_review(reviewed)) if status == "reviewing" else (True, "已忽略")
    except GapClaimLost:
        try:
            current = evolution_repo.k_gaps.find_one({"_id": oid}) or {}
            if disposition == "knowledge_missing" and current.get("candidate_id"):
                activate_gap_registrations(current)
                return True, current["candidate_id"]
        except Exception as exc:
            logger.warning(f"读取复核结果失败：{type(exc).__name__}")
        return False, "缺口状态已变化，请刷新后重试"
    except Exception as exc:
        logger.warning(f"缺口复核未完成：{type(exc).__name__}")
        return False, "处理未完成，系统将重试；请刷新查看状态"
