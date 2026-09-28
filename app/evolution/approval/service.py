"""审批状态机：draft → active / rejected / deprecated，通过时触发索引回流。

按 ``k_candidates._id`` 幂等操作；通过操作用 ``find_one_and_update`` 原子抢占，避免
并发对同一候选生成两个 ``evo_doc_id``。
"""
from __future__ import annotations

import uuid

from bson import ObjectId
from pymongo import ReturnDocument
from pymongo.errors import PyMongoError

from app.evolution.index.update import deactivate, upsert_item
from app.evolution.models import KnowledgeCandidate
from app.evolution.repositories import evolution_repo
from app.shared.runtime.logger import logger


def list_candidates(status: str | None = None, limit: int = 100) -> list[dict]:
    """按状态筛选候选（按时间倒序）。"""
    query = {"status": status} if status else {}
    try:
        return list(evolution_repo.k_candidates.find(query).sort("ts", -1).limit(limit))
    except PyMongoError as exc:
        logger.warning(f"查询候选失败：{exc}")
        return []


def get_candidate(candidate_id: str) -> dict | None:
    """按主键读取候选。"""
    try:
        return evolution_repo.k_candidates.find_one({"_id": ObjectId(candidate_id)})
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"获取候选失败：{exc}")
        return None


def approve(candidate_id: str, reason: str = "") -> bool:
    """审批通过：原子抢占 draft → active 并写入 Milvus；写失败则回滚状态。"""
    try:
        object_id = ObjectId(candidate_id)
    except Exception:  # noqa: BLE001 - 非法 id 视为不可审批
        return False

    evo_doc_id = f"evo_{uuid.uuid4().hex[:12]}"
    result = evolution_repo.k_candidates.find_one_and_update(
        {"_id": object_id, "status": "draft"},
        {"$set": {"status": "active", "evo_doc_id": evo_doc_id, "reason": reason}},
        return_document=ReturnDocument.AFTER,
    )
    if not result:
        # 非 draft（不存在 / 已 active / 已 reject）：已 active 视为幂等成功
        return evolution_repo.k_candidates.find_one({"_id": object_id, "status": "active"}) is not None

    item = KnowledgeCandidate(**{k: v for k, v in result.items() if k != "_id"})
    item.evo_doc_id = evo_doc_id
    item.status = "active"
    item.source_refs = item.source_refs or []
    if not upsert_item(evo_doc_id, item):
        # 向量写入失败：回滚抢占，避免残留 active 却检索不到
        try:
            evolution_repo.k_candidates.update_one(
                {"_id": object_id},
                {"$set": {"status": "draft", "evo_doc_id": None, "reason": ""}},
            )
        except PyMongoError as exc:
            logger.warning(f"审批回滚失败：{exc}")
        return False
    return True


def reject(candidate_id: str, reason: str = "") -> bool:
    """驳回候选。"""
    try:
        evolution_repo.k_candidates.update_one(
            {"_id": ObjectId(candidate_id)}, {"$set": {"status": "rejected", "reason": reason}}
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"驳回失败：{exc}")
        return False


def edit(candidate_id: str, *, faq_question: str | None = None, faq_answer: str | None = None) -> bool:
    """编辑候选内容（不改变状态，由再次 approve 生效）。"""
    patch: dict = {}
    if faq_question:
        patch["faq_question"] = faq_question
    if faq_answer:
        patch["faq_answer"] = faq_answer
    if not patch:
        return False
    try:
        evolution_repo.k_candidates.update_one({"_id": ObjectId(candidate_id)}, {"$set": patch})
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"编辑候选失败：{exc}")
        return False


def remove_candidate(candidate_id: str) -> bool:
    """删除候选（元数据 + 若已入库则下架向量）。"""
    doc = get_candidate(candidate_id)
    if doc and doc.get("evo_doc_id"):
        deactivate(doc["evo_doc_id"])
    try:
        evolution_repo.k_candidates.delete_one({"_id": ObjectId(candidate_id)})
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"删除候选失败：{exc}")
        return False
