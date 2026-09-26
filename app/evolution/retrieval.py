"""
进化条目召回：查询 kb_evolution_items（仅 active），供 RRF 融合。
失败返回空列表，绝不阻断主链路。结果形状对齐 embedding_search 的 embedding_chunks。
"""
from __future__ import annotations

from typing import Any

from app.infra.llm.providers import llm_providers
from app.infra.vector_store.milvus_gateway import milvus_gateway
from app.shared.runtime.logger import logger
from app.shared.utils.escape_milvus_string_utils import escape_milvus_string


def search_evolution_items(rewritten_query: str, item_names: list[str], limit: int = 10) -> list[dict[str, Any]]:
    if not rewritten_query:
        return []
    try:
        # 0. 集合可能尚未创建（无已审批 active 候选），跳过演进召回，避免每次检索刷 collection not found
        if not milvus_gateway.milvus_client.has_collection(collection_name=milvus_gateway.evolution_collection_name):
            return []
        # 1. 向量化改写问题
        result = llm_providers.generate_embeddings([rewritten_query])
        # 2. 过滤条件：仅 active；带商品名的候选只召回属于当前 item_name 的商品，
        #    但未打商品标签的候选（item_name 为空时写入端落为 default_item_name 占位符）
        #    属全局可召回，避免被商品过滤剔除（逐项转义，防 filter 注入）
        if item_names:
            in_list = ", ".join(f"'{escape_milvus_string(n)}'" for n in item_names)
            expr = f"status == 'active' and (item_name in [{in_list}] or item_name == 'default_item_name')"
        else:
            expr = "status == 'active'"
        reqs = milvus_gateway.create_requests(
            dense_vector=result["dense"][0],
            sparse_vector=result["sparse"][0],
            expr=expr,
            limit=max(limit * 2, 10),
        )
        milvus_result = milvus_gateway.hybrid_search(
            collection_name=milvus_gateway.evolution_collection_name,
            reqs=reqs,
            ranker_weights=(0.6, 0.4),
            norm_score=True,
            limit=limit,
            output_fields=[
                "evo_doc_id", "faq_question", "faq_answer", "source_refs", "item_name", "status"
            ],
        )
        raw_list = milvus_result[0] if milvus_result and len(milvus_result) > 0 else []
        return _format(raw_list)
    except Exception as e:
        logger.warning(f"进化条目召回失败，返回空: {e}")
        return []


def _format(milvus_list: list[dict]) -> list[dict[str, Any]]:
    items = []
    for item in milvus_list:
        entity = item.get("entity", {})
        items.append({
            "chunk_id": entity.get("evo_doc_id"),
            "score": item.get("distance", 0.0),
            "title": entity.get("faq_question"),
            "file_title": "__evolution__",
            "parent_title": "",
            "part": 0,
            "item_name": entity.get("item_name"),
            "content": entity.get("faq_answer"),
            "source": "evolution",
            "url": "",
            "type": "milvus",
        })
    return items