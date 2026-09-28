"""向量检索服务：按「改写后的问题 + 主体范围」做混合检索并回写 ``embedding_chunks``。"""
from __future__ import annotations

from app.process.query.agent.state import QueryGraphState
from app.rag.query.chunk_search import require_query_and_items, search_chunks, to_chunks


def search_by_milvus(item_names: list[str], rewritten_query: str) -> list:
    """兼容入口：供自进化调度器等按 ``(item_names, rewritten_query)`` 调用，返回原始命中。"""
    return search_chunks(item_names, rewritten_query)


def search_by_embedding(state: QueryGraphState, embedding: dict[str, list] | None = None) -> list:
    """执行知识库向量检索并返回业务 chunk 列表。

    :param embedding: 可选的已算好向量（与自进化召回共用，避免重复编码）
    """
    item_names, rewritten_query = require_query_and_items(state)
    milvus_list = search_chunks(item_names, rewritten_query, embedding=embedding)
    return to_chunks(milvus_list)
