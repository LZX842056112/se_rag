"""自进化路由：反馈提交、候选列表与审批写操作（统一挂载在 ``/api/evolution``）。

审批写操作要求请求头 ``X-Internal-Token`` 与 ``EVOLUTION_ADMIN_TOKEN`` 一致；
未配置 Token 时直接拒绝写操作。
"""
from __future__ import annotations

import os

from fastapi import APIRouter, Depends, Header

from app.api.errors import ApiError
from app.evolution.approval import service as approval_service
from app.evolution.feedback.collector import record_feedback
from app.evolution.models import FeedbackEvent
from app.evolution.repositories import evolution_repo
from app.evolution.schema import (
    CandidateApproveRequest,
    CandidateEditRequest,
    CandidateItem,
    CandidateListResponse,
    EvolutionStatusResponse,
    OkResponse,
)
from app.evolution.scheduler import loop_status
from app.evolution.online_eval.metrics import latest_metrics
from app.shared.config import settings
from app.shared.runtime.logger import logger

router = APIRouter(prefix="/api/evolution", tags=["evolution"])


def require_admin_token(x_internal_token: str = Header(default="")) -> None:
    """写操作鉴权依赖：校验 ``X-Internal-Token`` 与 ``EVOLUTION_ADMIN_TOKEN`` 一致。"""
    expected = os.getenv("EVOLUTION_ADMIN_TOKEN", "")
    if not expected:
        raise ApiError("admin_token_missing", "EVOLUTION_ADMIN_TOKEN 未配置", status_code=503)
    if x_internal_token != expected:
        raise ApiError("unauthorized", "鉴权失败", status_code=401)


def _to_item(doc: dict) -> CandidateItem:
    """Mongo 文档转对外候选项。"""
    return CandidateItem(
        id=str(doc.get("_id")),
        faq_question=doc.get("faq_question", ""),
        faq_answer=doc.get("faq_answer", ""),
        status=doc.get("status", ""),
        source_refs=doc.get("source_refs", []),
        item_names=doc.get("item_names", []),
        ts=doc.get("ts"),
    )


@router.post("/feedback", response_model=OkResponse)
def post_feedback(req: FeedbackEvent) -> OkResponse:
    """提交用户反馈（👍 / 👎）；未开启自进化时仅幂等接受。"""
    record_feedback(req)
    return OkResponse(code=200, message="feedback accepted")


@router.get("/candidates", response_model=CandidateListResponse)
def list_candidates(status: str | None = None, limit: int = 100) -> CandidateListResponse:
    """候选列表（可按 ``status`` 筛选）。"""
    docs = approval_service.list_candidates(status=status, limit=limit)
    return CandidateListResponse(items=[_to_item(doc) for doc in docs])


def _count_by(collection, field: str = "status") -> dict[str, int]:
    """按字段聚合计数（读不到库时返回空，不影响接口可用）。"""
    try:
        rows = collection.aggregate([{"$group": {"_id": f"${field}", "n": {"$sum": 1}}}])
        return {str(row.get("_id") or "unknown"): int(row.get("n", 0)) for row in rows}
    except Exception as exc:  # noqa: BLE001 - 状态接口属排障能力，缺库时降级
        logger.warning(f"闭环状态聚合失败（{field}）：{exc}")
        return {}


@router.get("/status", response_model=EvolutionStatusResponse)
def get_status() -> EvolutionStatusResponse:
    """闭环运行状态：缺口/候选计数、调度进度与最近一次指标快照。

    排障背景：用户曾只能靠翻日志判断「缺口为什么没扫到」；这里把调度游标与计数暴露成接口，
    审批页顶部直接展示，避免「明明在跑却像卡住」。
    """
    latest: dict | None = None
    try:
        rows = latest_metrics(recent=1)
        latest = rows[0] if rows else None
        if latest is not None:
            latest.pop("_id", None)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"读取最近指标快照失败：{exc}")
    feedback_events = 0
    try:
        feedback_events = evolution_repo.fb_events.count_documents({})
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"统计反馈事件失败：{exc}")
    return EvolutionStatusResponse(
        enabled=settings.evolution.enabled,
        scheduler=dict(loop_status()),
        gaps=_count_by(evolution_repo.k_gaps),
        candidates=_count_by(evolution_repo.k_candidates),
        feedback_events=feedback_events,
        latest_metric=latest,
    )


@router.post("/candidates/{candidate_id}/approve", response_model=OkResponse,
             dependencies=[Depends(require_admin_token)])
def approve_candidate(candidate_id: str, req: CandidateApproveRequest) -> OkResponse:
    """审批通过并把候选写入知识库（active）。"""
    ok, message = approval_service.approve(candidate_id, reason=req.reason)
    if not ok:
        code = "candidate_not_found" if message == "候选不存在" else "candidate_not_approvable"
        raise ApiError(code, message, status_code=404 if code == "candidate_not_found" else 400)
    return OkResponse(code=200, message="approved")


@router.post("/candidates/{candidate_id}/reject", response_model=OkResponse,
             dependencies=[Depends(require_admin_token)])
def reject_candidate(candidate_id: str, req: CandidateApproveRequest) -> OkResponse:
    """驳回候选。"""
    if not approval_service.reject(candidate_id, reason=req.reason):
        raise ApiError("candidate_not_found", "候选不存在", status_code=404)
    return OkResponse(code=200, message="rejected")


@router.post("/candidates/{candidate_id}/edit", response_model=OkResponse,
             dependencies=[Depends(require_admin_token)])
def edit_candidate(candidate_id: str, req: CandidateEditRequest) -> OkResponse:
    """编辑候选内容（问题/答案）。"""
    if not approval_service.edit(candidate_id, faq_question=req.faq_question, faq_answer=req.faq_answer):
        raise ApiError("edit_failed", "编辑失败或没有实际改动", status_code=400)
    return OkResponse(code=200, message="edited")


@router.delete("/candidates/{candidate_id}", response_model=OkResponse,
               dependencies=[Depends(require_admin_token)])
def remove_candidate(candidate_id: str) -> OkResponse:
    """下架候选：删除元数据，并从向量库移除已入库的条目。"""
    if not approval_service.remove_candidate(candidate_id):
        raise ApiError("candidate_not_found", "候选不存在或下架失败", status_code=404)
    return OkResponse(code=200, message="removed")
