"""
知识库分块检索公共模块。

供「直接向量检索」(embedding_search_service) 与「HyDE 假设性文档检索」
(hyde_search_service) 复用，消除两处逐行重复的校验 / 检索 / 格式化逻辑。
"""
from app.process.query.agent.state import QueryGraphState
from app.shared.runtime.logger import logger
from app.infra.llm.providers import llm_providers
from app.infra.vector_store.milvus_gateway import milvus_gateway
from app.rag.query.config import CHUNK_DENSE_METRIC
from app.rag.item_name.catalog import expand_item_names

# 检索输出字段（chunk 业务字段）
CHUNK_OUTPUT_FIELDS = [
    "chunk_id",
    "file_title",
    "title",
    "parent_title",
    "part",
    "item_name",
    "content",
]


def require_query_and_items(state: QueryGraphState) -> tuple[list[str], str]:
    """校验并取出 item_names / rewritten_query，任一为空即提前终止。"""
    item_names = state.get("item_names", [])
    rewritten_query = state.get("rewritten_query")
    if len(item_names) == 0 or not rewritten_query:
        logger.error("关联的主体或者重写的问题为空,业务无法继续,提前终止!")
        raise ValueError("关联的主体或者重写的问题为空,业务无法继续,提前终止!")
    return item_names, rewritten_query


def search_chunks(item_names: list[str], query_text: str) -> list:
    """
    对 query_text 做 BGE-M3 混合检索（稠密+稀疏），返回 Milvus 原始命中列表。

    item_names 为空时不加主体过滤，供自进化缺口上下文等全库轻量检索复用。
    非空时会先扩展为"同一实体的全部写法"，避免同一产品因多次导入留下近重复主体名
    时，只按其中一种写法过滤而漏掉另一种写法下的分片。
    """
    # 1. query_text 向量化
    result = llm_providers.generate_embeddings([query_text])
    # 2. 主体过滤名单：扩展为同一实体的全部写法
    filter_names = expand_item_names(item_names) if item_names else []
    # 3. 组装混合检索请求。dense 的 metric_type 必须与 kb_chunks 实际索引一致（HNSW/COSINE），
    #    否则 Milvus 报 "metric type not match"、检索静默失败（网关返回 None）-> 召回为空
    reqs = milvus_gateway.create_requests(
        dense_vector=result["dense"][0],
        sparse_vector=result["sparse"][0],
        dense_params={"metric_type": CHUNK_DENSE_METRIC},
        expr=f"item_name in {filter_names}" if filter_names else None,
        limit=5 * 2,
    )
    # 4. 混合检索
    milvus_result = milvus_gateway.hybrid_search(
        collection_name=milvus_gateway.chunk_collection_name,
        reqs=reqs,
        ranker_weights=(0.6, 0.4),
        norm_score=True,
        limit=5,
        output_fields=CHUNK_OUTPUT_FIELDS,
    )
    # 5. milvus_result = [[{id,distance,entity}]]，单列检索取第 0 组
    return milvus_result[0] if milvus_result and len(milvus_result) > 0 else []


def to_chunks(milvus_list) -> list[dict]:
    """将 Milvus 原始命中（{id,distance,entity}）统一格式化为业务 chunk 列表。"""
    chunks = []
    for item in milvus_list:
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
            "source": "milvus",
            "url": "",
        })
    return chunks
