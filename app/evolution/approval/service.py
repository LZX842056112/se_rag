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
from app.evolution.quality import looks_like_non_answer
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


def approve(candidate_id: str, reason: str = "") -> tuple[bool, str]:
    """审批通过：原子抢占 draft → active 并写入 Milvus；写失败则回滚状态。

    会拒绝两类「没有知识」的候选：
    - ``need_info``（生成器判定为无证据 / 无信息结论）；
    - 答案是「未提及…建议联系官方」这类无法作答型表述（必须先编辑补充事实）。

    :return: ``(是否成功, 失败原因)``；失败原因可直接回显给审批页面。
    """
    try:
        object_id = ObjectId(candidate_id)
    except Exception:  # noqa: BLE001 - 非法 id 视为不可审批
        return False, "候选 ID 不合法"

    current = evolution_repo.k_candidates.find_one({"_id": object_id})
    if current is None:
        return False, "候选不存在"
    if current.get("status") == "active":
        return True, ""  # 幂等：已入库视为成功
    if current.get("status") != "draft":
        if current.get("status") == "need_info":
            return False, "该候选没有可用事实，请先「编辑」补充答案后再通过"
        return False, f"候选当前状态为 {current.get('status')}，不可审批"
    if looks_like_non_answer(current.get("faq_answer")):
        return False, "候选答案未包含可入库的事实（疑似“无信息”结论），请先编辑补充后再通过"

    evo_doc_id = f"evo_{uuid.uuid4().hex[:12]}"
    result = evolution_repo.k_candidates.find_one_and_update(
        {"_id": object_id, "status": "draft"},
        {"$set": {"status": "active", "evo_doc_id": evo_doc_id, "reason": reason}},
        return_document=ReturnDocument.AFTER,
    )
    if not result:
        # 并发抢占失败：已 active 视为幂等成功，其余按状态不可审批处理
        if evolution_repo.k_candidates.find_one({"_id": object_id, "status": "active"}):
            return True, ""
        return False, "候选已被其他操作处理，请刷新后重试"

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
        return False, "写入向量库失败，候选已回滚为待审批"
    _mark_gap(item.gap_id, "resolved")
    return True, ""


def _mark_gap(gap_id: str | None, status: str) -> None:
    """回写来源缺口状态（审批通过 → resolved / 驳回 → rejected），避免缺口永久残留。"""
    if not gap_id:
        return
    try:
        evolution_repo.k_gaps.update_one({"_id": ObjectId(gap_id)}, {"$set": {"status": status}})
    except Exception as exc:  # noqa: BLE001 - 缺口状态属辅助信息，失败不影响审批结果
        logger.warning(f"回写缺口状态失败：{gap_id} -> {status}（{exc}）")


def reject(candidate_id: str, reason: str = "") -> bool:
    """驳回候选。"""
    try:
        object_id = ObjectId(candidate_id)
        current = evolution_repo.k_candidates.find_one({"_id": object_id}) or {}
        evolution_repo.k_candidates.update_one(
            {"_id": object_id}, {"$set": {"status": "rejected", "reason": reason}}
        )
        _mark_gap(current.get("gap_id"), "rejected")
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"驳回失败：{exc}")
        return False


def edit(candidate_id: str, *, faq_question: str | None = None, faq_answer: str | None = None) -> bool:
    """编辑候选内容。

    若原本是 ``need_info``（待人工补充）且这次补齐了可入库的事实，则自动回到 ``draft``，
    管理员即可直接「通过」；否则保持原状态。
    """
    patch: dict = {}
    if faq_question:
        patch["faq_question"] = faq_question
    if faq_answer:
        patch["faq_answer"] = faq_answer
    if not patch:
        return False
    try:
        object_id = ObjectId(candidate_id)
        current = evolution_repo.k_candidates.find_one({"_id": object_id}) or {}
        if current.get("status") == "need_info":
            merged_answer = patch.get("faq_answer") or current.get("faq_answer")
            if merged_answer and not looks_like_non_answer(merged_answer):
                patch["status"] = "draft"
                patch["reason"] = ""
        evolution_repo.k_candidates.update_one({"_id": object_id}, {"$set": patch})
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
