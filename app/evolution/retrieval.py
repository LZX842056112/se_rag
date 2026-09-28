"""自进化条目召回：查询 ``kb_evolution_items``（仅 active），供 RRF 融合。

失败返回空列表，绝不阻断主链路。返回结构与知识库 chunk 对齐，便于 RRF/重排统一处理。
"""
from __future__ import annotations

from typing import Any

from app.rag.item_name.catalog import expand_item_names
from app.rag.item_name.config import GLOBAL_ITEM_NAME
from app.evolution.quality import looks_like_non_answer
from app.shared.clients.milvus_gateway import eq_expr, in_expr, milvus_gateway
from app.shared.models import llm_providers
from app.shared.runtime.logger import logger
from app.shared.utils.text import name_equivalent

_EVOLUTION_OUTPUT_FIELDS = [
    "evo_doc_id",
    "faq_question",
    "faq_answer",
    "source_refs",
    "item_name",
    "status",
]


def _format(milvus_list: list[dict]) -> list[dict[str, Any]]:
    """把 Milvus 原始命中格式化为与知识库 chunk 同构的结构。

    历史数据里可能残留「未提及…建议联系官方」这类没有事实的条目（修复前误批入库的），
    它们被召回只会占掉引用位并让模型继续答“无法作答”，因此这里直接跳过。
    """
    items: list[dict[str, Any]] = []
    for item in milvus_list or []:
        entity = item.get("entity", {}) or {}
        answer = entity.get("faq_answer")
        if looks_like_non_answer(answer):
            logger.warning(
                f"跳过无事实的进化条目 {entity.get('evo_doc_id')}："
                f"{(entity.get('faq_question') or '')[:30]}"
            )
            continue
        items.append({
            "chunk_id": entity.get("evo_doc_id"),
            "score": item.get("distance", 0.0),
            "title": entity.get("faq_question"),
            "file_title": "__evolution__",
            "parent_title": "",
            "part": 0,
            "item_name": entity.get("item_name"),
            "content": answer,
            "source": "evolution",
            "type": "milvus",
            "url": "",
        })
    return items


def _search(rewritten_query: str, expr: str, limit: int, embedding: dict[str, list] | None) -> list[dict]:
    """按给定过滤表达式做一次混合检索并格式化结果。"""
    vectors = embedding or llm_providers.embed_text(rewritten_query)
    reqs = milvus_gateway.create_requests(
        dense_vector=vectors["dense"][0],
        sparse_vector=vectors["sparse"][0],
        expr=expr,
        limit=max(limit * 2, 10),
    )
    milvus_result = milvus_gateway.hybrid_search(
        collection_name=milvus_gateway.evolution_collection_name,
        reqs=reqs,
        ranker_weights=(0.6, 0.4),
        norm_score=True,
        limit=limit,
        output_fields=_EVOLUTION_OUTPUT_FIELDS,
    )
    return _format(milvus_result[0] if milvus_result else [])


def search_evolution_items(
    rewritten_query: str,
    item_names: list[str],
    limit: int = 10,
    embedding: dict[str, list] | None = None,
) -> list[dict[str, Any]]:
    """召回与当前问题相关的已审批进化条目（``status == 'active'``）。

    :param embedding: 已算好的查询向量（与知识库检索共用，避免重复编码）
    """
    if not rewritten_query:
        return []
    try:
        if not milvus_gateway.milvus_client.has_collection(
            collection_name=milvus_gateway.evolution_collection_name
        ):
            # 集合尚未创建（还没有审批通过的条目），直接跳过，避免每次检索刷 "collection not found"
            return []

        active_expr = eq_expr("status", "active")
        if item_names:
            # 带主体：按「同一实体的全部已知写法」过滤，并放行未打标签的全局条目
            names = expand_item_names(item_names)
            expr = f"{active_expr} and ({in_expr('item_name', names)} or {eq_expr('item_name', GLOBAL_ITEM_NAME)})"
            hits = _search(rewritten_query, expr, limit, embedding)
            if hits:
                return hits
            # 目录不可用等兜底：放开主体过滤重查，再按 token 前缀等价在本地裁决，
            # 避免跨商品误召回（主体名口径不一致时仍能召回权威 FAQ）
            wide = _search(rewritten_query, active_expr, max(limit * 4, 40), embedding)
            return [h for h in wide if name_equivalent(h.get("item_name"), item_names, global_item=GLOBAL_ITEM_NAME)][:limit]

        return _search(rewritten_query, active_expr, limit, embedding)
    except Exception as exc:  # noqa: BLE001 - 进化召回失败必须降级为空
        logger.warning(f"进化条目召回失败，返回空：{exc}")
        return []
