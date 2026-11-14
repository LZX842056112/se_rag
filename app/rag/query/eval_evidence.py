"""评测证据：把本轮检索结果整理成可回传的结构。

背景：离线评测通过 HTTP 调用 /api/query。默认响应只带引用元数据，缺少正文与分层
chunk_id，无法计算 Recall@k、NDCG 或 RAGAS 的上下文指标。请求显式带上
``include_evidence=true`` 时才附加本模块的输出，避免普通问答响应体膨胀。
"""
from __future__ import annotations

from typing import Any

# 单次回传的证据上限：评测只需要 Top-N，过大既拖慢响应也无意义
_MAX_EVIDENCE_DOCS = 20

def _chunk_id(doc: dict) -> str:
    value = doc.get("chunk_id")
    return str(value) if value is not None else ""


def chunk_ids(docs: list | None) -> list[str]:
    """按输入顺序保留有 chunk_id 的条目并去重。"""
    return list(dict.fromkeys(chunk_id for doc in docs or []
                              if isinstance(doc, dict) and (chunk_id := _chunk_id(doc))))


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
    mode = state.get("query_mode")
    if mode == "legacy":
        layers["embedding"] = chunk_ids(state.get("embedding_chunks"))
        if state.get("keyword_chunks") is not None:
            layers["keyword"] = chunk_ids(state["keyword_chunks"])
        if state.get("hyde_executed"):
            layers["hyde"] = chunk_ids(state.get("hyde_embedding_chunks"))
        layers["rrf"] = chunk_ids(state.get("rrf_chunks"))
        for name in ("manual_rerank_input", "local_rerank_input"):
            ids = state.get(f"{name}_chunk_ids")
            if ids is not None:
                layers[name] = ids
        if state.get("reranked_chunk_ids") is not None:
            layers["reranked"] = state["reranked_chunk_ids"]
        if state.get("selected_chunk_ids") is not None:
            layers["selected"] = state["selected_chunk_ids"]
    elif mode == "agentic":
        layers["agentic_retrieval"] = state.get("agentic_retrieval_chunk_ids", [])
    if layers:
        layers["final"] = chunk_ids(state.get("reranked_docs"))
    return layers


def build_evaluation(state: dict[str, Any]) -> dict[str, Any]:
    """输出查询的实际路径、结果及生成模型收到的证据片段。"""
    outcome = state.get("query_outcome")
    if state.get("handoff_required"):
        outcome = "handoff"
    elif not outcome:
        if state.get("intent") == "chitchat":
            outcome = "chitchat"
        elif state.get("item_name_options"):
            outcome = "clarification"
        else:
            outcome = "answered"
    return {
        "query_mode": state.get("query_mode") or "none",
        "outcome": outcome,
        "contexts": state.get("evaluation_contexts") or state.get("answer_contexts") or [],
    }
