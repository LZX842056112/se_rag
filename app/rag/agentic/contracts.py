"""检索、答案生成与评估共享的数据契约。"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class RetrievalScope(BaseModel):
    """服务器所选的产品以及可选的语料限制。"""

    model_config = ConfigDict(extra="forbid")
    item_names: list[str] = Field(default_factory=list)
    corpus_version: str | None = None
    allowed_document_kinds: list[str] = Field(default_factory=list)


class EvidenceRef(BaseModel):
    """精确检索得到的上下文及其来源标识。"""

    model_config = ConfigDict(extra="forbid")
    evidence_id: str
    source: Literal["kb", "evolution", "web"]
    doc_id: str | None = None
    chunk_id: str | None = None
    parent_id: str | None = None
    item_name: str | None = None
    text: str
    content_sha256: str
    page: int | None = None
    document_version: str | None = None
    document_kind: str | None = None
    valid_until: float | None = None
    approval_status: str | None = None
    source_version: str | None = None
    fetched_at: float | None = None
    title: str | None = None
    url: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ToolResult(BaseModel):
    """空结果与服务失败具有不同的状态。"""

    status: Literal["ok", "empty", "timeout", "unavailable", "invalid"]
    evidence: list[EvidenceRef] = Field(default_factory=list)
    elapsed_ms: float = Field(default=0.0, ge=0)
    token_count: int = Field(default=0, ge=0)
    error: str | None = None