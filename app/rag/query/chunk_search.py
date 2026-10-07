"""知识库分块检索公共模块。

供「直接向量检索」(``embedding_search_service``) 与「HyDE 假设性文档检索」
(``hyde_search_service``) 复用，消除两处重复的校验 / 检索 / 格式化逻辑。

性能要点：``search_chunks`` 允许传入已算好的向量（``embedding``），使同一次提问的
多路检索（知识库 / 自进化条目）只做一次向量化，而不是各算一次。
"""
from __future__ import annotations

from app.rag.item_name.catalog import expand_item_names
from app.rag.query.config import CHUNK_DENSE_METRIC
from app.shared.clients.milvus_gateway import in_expr, milvus_gateway
from app.shared.models import llm_providers
from app.shared.runtime.logger import logger
from app.shared.utils.require import require_state_list, require_state_str

# 检索输出字段（chunk 业务字段）
CHUNK_OUTPUT_FIELDS = [
    "chunk_id",
    "doc_id",
    "parent_id",
    "file_title",
    "title",
    "parent_title",
    "heading_path",
    "part",
    "seq",
    "page",
    "item_name",
    "content",
]

# 每路召回候选数与最终返回条数
SEARCH_CANDIDATE_LIMIT = 10
SEARCH_TOP_K = 5


def require_query_and_items(state) -> tuple[list[str], str]:
    """校验并取出 ``item_names`` / ``rewritten_query``，任一为空即提前终止。"""
    item_names = require_state_list(state, "item_names")
    rewritten_query = require_state_str(state, "rewritten_query")
    return item_names, rewritten_query


def search_chunks(
    item_names: list[str],
    query_text: str,
    *,
    embedding: dict[str, list] | None = None,
    limit: int = SEARCH_TOP_K,
    collection_name: str | None = None,
    output_fields: list[str] | None = None,
    dense_metric: str = CHUNK_DENSE_METRIC,
) -> list:
    """对 ``query_text`` 做 BGE-M3 混合检索（稠密+稀疏），返回 Milvus 原始命中列表。

    :param item_names: 主体过滤名单；为空时不加主体过滤（供自进化缺口上下文等复用）
    :param embedding: 已算好的单条向量；为空时内部自行向量化
    :param collection_name: 目标集合；默认知识库分块集合
    """
    # 1. 向量化（复用调用方传入的向量，避免同一次提问重复编码）
    vectors = embedding or llm_providers.embed_text(query_text)

    # 2. 主体过滤名单：扩展为同一实体的全部写法，避免近重复主体名导致漏召回
    filter_names = expand_item_names(item_names) if item_names else []
    expr = in_expr("item_name", filter_names) if filter_names else None

    # 3. 组装混合检索请求。dense 的 metric_type 必须与目标集合实际索引一致，
    #    否则 Milvus 报 "metric type not match"、网关静默返回 None -> 召回为空
    reqs = milvus_gateway.create_requests(
        dense_vector=vectors["dense"][0],
        sparse_vector=vectors["sparse"][0],
        dense_params={"metric_type": dense_metric},
        expr=expr,
        limit=max(limit * 2, SEARCH_CANDIDATE_LIMIT),
    )

    # 4. 混合检索（返回 [[{id,distance,entity}, ...]]，单列检索取第 0 组）
    milvus_result = milvus_gateway.hybrid_search(
        collection_name=collection_name or milvus_gateway.chunk_collection_name,
        reqs=reqs,
        ranker_weights=(0.6, 0.4),
        norm_score=True,
        limit=limit,
        output_fields=output_fields or CHUNK_OUTPUT_FIELDS,
    )
    return milvus_result[0] if milvus_result else []


def to_chunks(milvus_list) -> list[dict]:
    """把 Milvus 原始命中（``{id,distance,entity}``）统一格式化为业务 chunk 列表。"""
    chunks = []
    for item in milvus_list or []:
        entity = item.get("entity", {})
        chunks.append({
            "chunk_id": entity.get("chunk_id"),
            "score": item.get("distance", 0.0),
            "title": entity.get("title"),
            "file_title": entity.get("file_title"),
            "parent_title": entity.get("parent_title"),
            "part": entity.get("part"),
            "item_name": entity.get("item_name"),
            "content": entity.get("content"),
            # 父块回溯所需字段：parent_id 用于批量取回章节背景，page/heading_path 用于引用溯源
            "doc_id": entity.get("doc_id"),
            "parent_id": entity.get("parent_id"),
            "heading_path": entity.get("heading_path"),
            "seq": entity.get("seq"),
            "page": entity.get("page"),
            "source": "milvus",
            "type": "milvus",
            "url": "",
        })
    logger.debug(f"知识库检索命中 {len(chunks)} 条")
    return chunks
