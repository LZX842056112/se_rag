"""
自进化对外 API 的 Pydantic 请求 / 响应模型，风格与 query_schema.py 保持一致。
反馈请求复用 models.FeedbackEvent 作为唯一模型（避免 API 与持久化双份定义）。
"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


# ---------------- 候选（审批后台） ---------------
class CandidateItem(BaseModel):
    id: str
    faq_question: str
    faq_answer: str
    status: str
    source_refs: list[str] = Field(default_factory=list)
    item_names: list[str] = Field(default_factory=list)
    ts: Any = None


class CandidateListResponse(BaseModel):
    items: list[CandidateItem] = Field(default_factory=list)


class CandidateApproveRequest(BaseModel):
    reason: str = ""


class CandidateEditRequest(BaseModel):
    faq_question: Optional[str] = None
    faq_answer: Optional[str] = None
    reason: str = ""


# ---------------- 引用（读链路返回） ---------------
class CitationModel(BaseModel):
    faq_id: str
    score: float = 0.0
    source: str = "kb"


class OkResponse(BaseModel):
    code: int = 200
    message: str = "ok"