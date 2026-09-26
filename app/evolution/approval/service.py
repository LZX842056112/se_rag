"""
审批状态机：draft → approved(rejected/deprecated)。approved 触发索引 upsert。
按 k_candidates._id 幂等操作。
"""
from __future__ import annotations

import uuid

from bson import ObjectId
from pymongo import ReturnDocument
from pymongo.errors import PyMongoError

from app.evolution.index.update import deactivate, upsert_item
from app.evolution.models import KnowledgeCandidate
from app.evolution.repositories import get_evolution_mongo_tool
from app.shared.runtime.logger import logger


def list_candidates(status: str | None = None, limit: int = 100) -> list[dict]:
    repo = get_evolution_mongo_tool()
    query = {"status": status} if status else {}
    try:
        return list(repo.k_candidates.find(query).sort("ts", -1).limit(limit))
    except PyMongoError as e:
        logger.warning(f"查询候选失败: {e}")
        return []


def get_candidate(candidate_id: str) -> dict | None:
    try:
        return get_evolution_mongo_tool().k_candidates.find_one({"_id": ObjectId(candidate_id)})
    except Exception as e:
        logger.warning(f"获取候选失败: {e}")
        return None


def approve(candidate_id: str, reviewer: str = "admin", reason: str = "") -> bool:
    """审批通过：原子抢占 draft → active，生成 evo_doc_id 并写 Milvus。

    用 find_one_and_update 原子抢占，避免并发对同一候选生成两个 evo_doc_id。
    """
    repo = get_evolution_mongo_tool()
    try:
        cid = ObjectId(candidate_id)
    except Exception:
        return False
    evo_doc_id = f"evo_{uuid.uuid4().hex[:12]}"
    # 仅 draft 可抢占；返回抢占后文档
    res = repo.k_candidates.find_one_and_update(
        {"_id": cid, "status": "draft"},
        {"$set": {"status": "active", "evo_doc_id": evo_doc_id, "reason": reason}},
        return_document=ReturnDocument.AFTER,
    )
    if not res:
        # 非 draft（不存在/已 active/已 reject）：已 active 视为幂等成功
        if repo.k_candidates.find_one({"_id": cid, "status": "active"}):
            return True
        return False
    item = KnowledgeCandidate(**{k: v for k, v in res.items() if k != "_id"})
    item.evo_doc_id = evo_doc_id
    item.status = "active"
    item.source_refs = item.source_refs or []
    if not upsert_item(evo_doc_id, item):
        # Milvus 写入失败：回滚抢占，避免残留 active 却无向量
        try:
            repo.k_candidates.update_one(
                {"_id": cid},
                {"$set": {"status": "draft", "evo_doc_id": None, "reason": ""}},
            )
        except PyMongoError as e:
            logger.warning(f"审批回滚失败: {e}")
        return False
    return True


def reject(candidate_id: str, reason: str = "") -> bool:
    try:
        get_evolution_mongo_tool().k_candidates.update_one(
            {"_id": ObjectId(candidate_id)}, {"$set": {"status": "rejected", "reason": reason}}
        )
        return True
    except Exception as e:
        logger.warning(f"驳回失败: {e}")
        return False


def edit(candidate_id: str, *, faq_question: str | None = None, faq_answer: str | None = None) -> bool:
    """编辑候选内容（暂不落集合，由再次 approve 生效）。"""
    patch: dict = {}
    if faq_question:
        patch["faq_question"] = faq_question
    if faq_answer:
        patch["faq_answer"] = faq_answer
    if not patch:
        return False
    try:
        get_evolution_mongo_tool().k_candidates.update_one(
            {"_id": ObjectId(candidate_id)}, {"$set": patch}
        )
        return True
    except Exception as e:
        logger.warning(f"编辑候选失败: {e}")
        return False


def remove_candidate(candidate_id: str) -> bool:
    """删除候选（元数据 + 集合下架）。"""
    doc = get_candidate(candidate_id)
    if doc and doc.get("evo_doc_id"):
        deactivate(doc["evo_doc_id"])
    try:
        get_evolution_mongo_tool().k_candidates.delete_one({"_id": ObjectId(candidate_id)})
        return True
    except Exception as e:
        logger.warning(f"删除候选失败: {e}")
        return False