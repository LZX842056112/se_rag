"""带来源校验与可恢复发布任务的候选管理。"""
from __future__ import annotations

from bson import ObjectId
from pymongo.errors import PyMongoError

from app.evolution.index.update import deactivate, upsert_item
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
    """校验来源，并通过可恢复的、带租约的任务发布。"""
    from app.evolution.governance import approve_candidate
    ok, message = approve_candidate(candidate_id, reason)
    if ok:
        doc = get_candidate(candidate_id) or {}
        _mark_gap(doc.get("gap_id"), "resolved")
    return ok, message

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
        from app.evolution.flags import writes_enabled
        from app.evolution.governance import revoke_candidate
        if not writes_enabled():
            return False
        object_id = ObjectId(candidate_id)
        current = evolution_repo.k_candidates.find_one({"_id": object_id}) or {}
        if not current or not revoke_candidate(candidate_id, reason):
            return False
        evolution_repo.k_candidates.update_one(
            {"_id": object_id, 'publication_status': 'revoked'},
            {"$set": {"status": "rejected", "reason": reason}}
        )
        _mark_gap(current.get("gap_id"), "rejected")
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"驳回失败：{exc}")
        return False


def edit(candidate_id: str, *, faq_question: str | None = None, faq_answer: str | None = None,
         source_refs: list[str] | None = None, item_names: list[str] | None = None) -> bool:
    """编辑候选内容。

    若原本是 ``need_info``（待人工补充）且这次补齐了可入库的事实，则自动回到 ``draft``，
    管理员即可直接「通过」；否则保持原状态。
    """
    from app.evolution.flags import writes_enabled
    from app.evolution.governance import revoke_candidate
    if not writes_enabled():
        return False
    patch: dict = {}
    if faq_question:
        patch["faq_question"] = faq_question
    if faq_answer:
        patch["faq_answer"] = faq_answer
    if source_refs is not None:
        patch["source_refs"] = list(dict.fromkeys(source_refs))
    if item_names is not None:
        patch["item_names"] = item_names
    if not patch:
        return False
    try:
        object_id = ObjectId(candidate_id)
        current = evolution_repo.k_candidates.find_one({"_id": object_id}) or {}
        if not current:
            return False
        if source_refs is not None or item_names is not None:
            from app.evolution.governance import validate_sources
            merged = {**current, **patch, "source_versions": []}
            patch["source_versions"] = validate_sources(merged, check_support=False)
            patch["schema_version"] = 1
        if current.get("evo_doc_id"):
            revoke_candidate(candidate_id, "content edited")
        patch.update({"publication_status": "draft", "content_version": int(current.get("content_version", 0)) + 1,
                      "status": "draft" if current.get("status") != "need_info" else "need_info"})
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
    from app.evolution.flags import writes_enabled
    from app.evolution.governance import revoke_candidate
    if not writes_enabled():
        return False
    doc = get_candidate(candidate_id) or {}
    revoke_candidate(candidate_id, "candidate removed", process_task=False)
    if doc.get("evo_doc_id"):
        deactivate(doc["evo_doc_id"])
    try:
        evolution_repo.k_candidates.delete_one({"_id": ObjectId(candidate_id)})
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"删除候选失败：{exc}")
        return False