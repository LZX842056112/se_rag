from app.process.query.agent.state import QueryGraphState
from app.rag.query.chunk_search import (
    require_query_and_items,
    search_chunks,
    to_chunks,
)


def search_by_milvus(item_names: list[str], rewritten_query: str):
    """兼容入口：供自进化调度器等按 (item_names, rewritten_query) 调用。"""
    return search_chunks(item_names, rewritten_query)


def search_by_embedding(state: QueryGraphState) -> QueryGraphState:
    """
    向量检索服务：
    1. 根据改写后的问题和限定的商品范围
    2. 利用 BGEM3 混合检索（稠密+稀疏）技术
    3. 从 Milvus 向量数据库中召回 Top-K 最相关的知识切片
    4. 回写 embedding_chunks
    """
    # 1. 获取并校验参数(state) -> item_names rewritten_query
    item_names, rewritten_query = require_query_and_items(state)
    # 2. 进行向量的混合+条件检索
    milvus_list = search_chunks(item_names, rewritten_query)
    # 3. 进行结果的统一格式化处理
    return to_chunks(milvus_list)
