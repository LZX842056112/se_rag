"""评测证据：把本轮检索结果整理成可回传的结构。

背景：离线评测通过 HTTP 调用 /api/query。默认响应只带引用元数据，缺少正文与分层
chunk_id，无法计算 Recall@k、NDCG 或 RAGAS 的上下文指标。请求显式带上
``include_evidence=true`` 时才附加本模块的输出，避免普通问答响应体膨胀。
"""
from __future__ import annotations

from typing import Any

# 单次回传的证据上限：评测只需要 Top-N，过大既拖慢响应也无意义
_MAX_EVIDENCE_DOCS = 20

# 分层名称与查询图 state 的字段一一对应（顺序即检索顺序）
LAYER_FIELDS: tuple[tuple[str, str], ...] = (
    ("embedding", "embedding_chunks"),
    ("hyde", "hyde_embedding_chunks"),
    ("rrf", "rrf_chunks"),
    ("reranked", "reranked_docs"),
)


def _chunk_id(doc: dict) -> str:
    value = doc.get("chunk_id")
    return str(value) if value is not None else ""


def _dedupe_keep_order(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def build_evidence(docs: list | None) -> list[dict[str, Any]]:
    """把重排后的文档整理成证据列表（去重、限量、只保留评测需要的字段）。"""
    evidence: list[dict[str, Any]] = []
    seen: set[str] = set()
    for doc in docs or []:
        if not isinstance(doc, dict):
            continue
        chunk_id = _chunk_id(doc)
        # 联网结果没有 chunk_id，评测检索指标时不计入
        if not chunk_id or chunk_id in seen:
            continue
        seen.add(chunk_id)
        evidence.append({
            "chunk_id": chunk_id,
            # 重排文档用 text 存正文，检索层用 content；两者都兼容
            "content": str(doc.get("content") or doc.get("text") or ""),
            "score": doc.get("score"),
            "source": str(doc.get("source") or "kb"),
            "title": str(doc.get("title") or ""),
            "file_title": str(doc.get("file_title") or ""),
            "item_name": str(doc.get("item_name") or ""),
            "page": doc.get("page"),
            "heading_path": str(doc.get("heading_path") or ""),
            "document_version": str(doc.get("document_version") or ""),
        })
        if len(evidence) >= _MAX_EVIDENCE_DOCS:
            break
    return evidence


def build_retrieval_layers(state: dict[str, Any]) -> dict[str, list[str]]:
    """按检索层输出 chunk_id 列表，用于分层 Recall@k / MRR / NDCG。"""
    layers: dict[str, list[str]] = {}
    for name, field in LAYER_FIELDS:
        docs = state.get(field) or []
        layers[name] = _dedupe_keep_order([_chunk_id(doc) for doc in docs if isinstance(doc, dict)])
    return layers
