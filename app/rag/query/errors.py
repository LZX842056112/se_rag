"""查询链路的错误分类：把异常映射成稳定的机器码（查询侧排障用）。

背景：历史实现把所有异常统一为 ``query_failed``，运维无法区分「Milvus 挂了」
「LLM 超时」「重排 OOM」「Mongo 挂」。此处按异常类型（优先）与消息关键字
（兜底）归类，码值经 SSE error 事件与任务状态暴露，HTTP 契约不变
（非流式仍返回 ``query_failed`` / 500）。

与 ``app.rag.import_.errors``：复用「类型优先 + 关键字兜底」的机制与 snake_case
码风格，但**不共用规则表**——导入域规则（mineru/切分/对账）用于查询侧会把
LLM 超时错分成 mineru_timeout、把 Milvus 挂错分成 index_failed。
"""
from __future__ import annotations

import grpc
import httpx
from openai import APIError, APITimeoutError
from pymilvus.exceptions import MilvusException
from pymongo.errors import PyMongoError

# 兜底码：与现有 HTTP/SSE 契约一致（无法归类时保持现状口径）
DEFAULT_CODE = "query_failed"

# 关键字兜底规则：按顺序匹配，先具体后笼统
_KEYWORD_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("milvus_unavailable", ("milvus", "grpc", "向量库")),
    ("mongo_unavailable", ("mongo",)),
    ("rerank_failed", ("重排", "rerank")),
    ("embedding_failed", ("向量", "embedding", "bge")),
    ("llm_timeout", ("超时", "timeout")),
    ("llm_failed", ("模型", "llm", "openai", "大模型")),
)


def classify_query_error(exc: BaseException) -> tuple[str, str]:
    """把异常映射为 ``(错误码, 中文提示)``；无法归类时返回 ``("query_failed", 原始消息)``。"""
    message = str(exc) or exc.__class__.__name__

    if isinstance(exc, (grpc.RpcError, MilvusException)):
        return "milvus_unavailable", message
    if isinstance(exc, PyMongoError):
        return "mongo_unavailable", message
    if isinstance(exc, (TimeoutError, APITimeoutError, httpx.TimeoutException)):
        return "llm_timeout", message
    if isinstance(exc, (APIError, httpx.NetworkError, httpx.RemoteProtocolError)):
        return "llm_failed", message

    lowered = message.lower()
    for code, keywords in _KEYWORD_RULES:
        if any(keyword in lowered for keyword in keywords):
            return code, message
    return DEFAULT_CODE, message
