"""
审批后台 API：/evolution/candidates 相关只读 + 操作。
写操作（approve/reject/edit）通过头部 Token 鉴权，防未授权改动。
"""
import os

from fastapi import APIRouter, Depends, Header, HTTPException

from app.evolution.approval import service
from app.evolution.schema import CandidateApproveRequest, CandidateEditRequest, CandidateItem, CandidateListResponse, \
    OkResponse

approval_router = APIRouter(prefix="/evolution", tags=["evolution-approval"])


def require_admin_token(x_internal_token: str = Header(default="")) -> None:
    """写操作鉴权依赖：校验 X-Internal-Token 与 EVOLUTION_ADMIN_TOKEN 一致。"""
    expected = os.getenv("EVOLUTION_ADMIN_TOKEN", "")
    if not expected:
        raise HTTPException(status_code=503, detail="EVOLUTION_ADMIN_TOKEN 未配置")
    if x_internal_token != expected:
        raise HTTPException(status_code=401, detail="鉴权失败")


def _to_item(doc: dict) -> CandidateItem:
    return CandidateItem(
        id=str(doc.get("_id")),
        faq_question=doc.get("faq_question", ""),
        faq_answer=doc.get("faq_answer", ""),
        status=doc.get("status", ""),
        source_refs=doc.get("source_refs", []),
        item_names=doc.get("item_names", []),
        ts=doc.get("ts"),
    )


@approval_router.get("/candidates", response_model=CandidateListResponse)
def list_candidates(status: str | None = None, limit: int = 100) -> CandidateListResponse:
    docs = service.list_candidates(status=status, limit=limit)
    return CandidateListResponse(items=[_to_item(d) for d in docs])


@approval_router.post("/candidates/{candidate_id}/approve", response_model=OkResponse,
                      dependencies=[Depends(require_admin_token)])
def approve_candidate(candidate_id: str, req: CandidateApproveRequest) -> OkResponse:
    if not service.approve(candidate_id, reason=req.reason):
        raise HTTPException(status_code=404, detail="candidate not found or not approvable")
    return OkResponse(code=200, message="approved")


@approval_router.post("/candidates/{candidate_id}/reject", response_model=OkResponse,
                      dependencies=[Depends(require_admin_token)])
def reject_candidate(candidate_id: str, req: CandidateApproveRequest) -> OkResponse:
    if not service.reject(candidate_id, reason=req.reason):
        raise HTTPException(status_code=404, detail="candidate not found")
    return OkResponse(code=200, message="rejected")


@approval_router.post("/candidates/{candidate_id}/edit", response_model=OkResponse,
                      dependencies=[Depends(require_admin_token)])
def edit_candidate(candidate_id: str, req: CandidateEditRequest) -> OkResponse:
    if not service.edit(candidate_id, faq_question=req.faq_question, faq_answer=req.faq_answer):
        raise HTTPException(status_code=400, detail="edit failed or empty change")
    return OkResponse(code=200, message="edited")
