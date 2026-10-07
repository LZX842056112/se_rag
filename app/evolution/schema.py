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
    source: str = "kb"  # kb | evolution | web
    # 联网引用没有 chunk_id，用标题（退化时用 URL）给用户看
    title: str = ""
    # 页码与章节面包屑：来自切分阶段的结构化元数据，供引用溯源
    page: int | None = None
    heading: str = ""


class OkResponse(BaseModel):
    code: int = 200
    message: str = "ok"


# ---------------- 闭环运行状态（排障用） ---------------
class EvolutionStatusResponse(BaseModel):
    """自进化闭环的可观测状态：计数 + 调度进度 + 最近一次指标快照。"""

    enabled: bool
    scheduler: dict[str, Any] = Field(default_factory=dict)
    gaps: dict[str, int] = Field(default_factory=dict)
    candidates: dict[str, int] = Field(default_factory=dict)
    feedback_events: int = 0
    latest_metric: dict[str, Any] | None = None
