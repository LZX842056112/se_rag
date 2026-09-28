"""
自进化事件 / 缺口 / 候选 / 指标 / 参数的数据结构（Pydantic + Mongo 文档映射）。
字段命名与改造方案 §4.2 保持一致。
"""
from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel, Field


def _now() -> float:
    return time.time()


# ---------------- 反馈事件 fb_events ----------------
class FeedbackEvent(BaseModel):
    session_id: str
    query: str = ""
    rewritten_query: str = ""
    cited_chunk_ids: list[str] = Field(default_factory=list)
    item_names: list[str] = Field(default_factory=list)  # 查询已识别的商品名，供缺口/候选透传
    adopt: bool | None = None          # None=未知, True=采纳(点赞/命中), False=拒绝(点踩/差评)
    thumbs: int | None = None          # +1 点赞 / -1 点踩 / 0 无
    source: str = "kb"                 # web | kb | evolution | hyde
    ts: float = Field(default_factory=_now)

    def document(self) -> dict[str, Any]:
        return self.model_dump()


# ---------------- 知识缺口 k_gaps ----------------
class GapSignal(BaseModel):
    user: float = 0.0          # 点踩/转人工/差评
    retrieval: float = 0.0     # 零命中/低置信/无检索直达
    generation: float = 0.0    # 接地性低/未解决
    confidence: float = 0.0    # 最终加权置信度
    grade: str = "none"        # strong | weak | none


class KnowledgeGap(BaseModel):
    gap_id: str | None = None   # Mongo 主键（生成候选时透传给候选，用于回写缺口状态）
    session_id: str
    query: str = ""
    item_names: list[str] = Field(default_factory=list)
    confidence: float = 0.0
    signals: GapSignal = Field(default_factory=GapSignal)
    transcript_slice: str = ""
    status: str = "pending"    # pending | candidate | rejected
    ts: float = Field(default_factory=_now)

    def document(self) -> dict[str, Any]:
        return self.model_dump()


# ---------------- 知识候选 k_candidates ----------------
class KnowledgeCandidate(BaseModel):
    faq_question: str
    faq_answer: str
    source_refs: list[str] = Field(default_factory=list)  # 来源 chunk_id / 文档
    item_names: list[str] = Field(default_factory=list)
    status: str = "draft"      # draft | active | deprecated | rejected
    evo_doc_id: str | None = None
    reason: str = ""
    gap_id: str | None = None   # 来源缺口 id（审批后把缺口标记为 resolved/rejected）
    ts: float = Field(default_factory=_now)

    def document(self) -> dict[str, Any]:
        return self.model_dump()


# ---------------- 指标快照 k_metrics ----------------
class MetricSnapshot(BaseModel):
    ts: float = Field(default_factory=_now)
    recall_k: float = 0.0
    precision: float = 0.0
    groundedness: float = 0.0
    adopt_rate: float = 0.0
    gap_rate: float = 0.0
    params_snapshot: dict[str, Any] = Field(default_factory=dict)

    def document(self) -> dict[str, Any]:
        return self.model_dump()


# ---------------- 参数注册表 param_registry ----------------
class ParamRecord(BaseModel):
    key: str
    value: Any
    updated_at: float = Field(default_factory=_now)
    updated_by: str = "manual"    # metric | manual
    rev: int = 0

    def document(self) -> dict[str, Any]:
        return self.model_dump()
