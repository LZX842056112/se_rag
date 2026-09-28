"""查询图节点：知识库向量检索 + 自进化条目召回。

两路检索复用同一份查询向量（``rewritten_query`` 只向量化一次），避免旧实现里
同一段文本被编码两次的开销。
"""
from app.evolution.retrieval import search_evolution_items
from app.rag.query.embedding_search_service import search_by_embedding
from app.shared.config import settings
from app.shared.models import llm_providers
from app.shared.runtime.logger import logger, node_log
from app.shared.utils.task_state import track_node_task


@node_log("node_search_embedding")
@track_node_task("node_search_embedding")
def node_search_embedding(state):
    """知识库向量检索，并在自进化开启时召回演进条目。"""
    rewritten_query = state.get("rewritten_query", "")
    # 查询向量只算一次，供知识库与自进化两路检索共用
    embedding = llm_providers.embed_text(rewritten_query) if rewritten_query else None

    embedding_chunks = search_by_embedding(state, embedding=embedding)

    evolution_chunks: list = []
    if settings.evolution.enabled:
        try:
            evolution_chunks = search_evolution_items(
                rewritten_query=rewritten_query,
                item_names=state.get("item_names", []),
                limit=settings.evolution.recall_limit,
                embedding=embedding,
            )
        except Exception as exc:  # noqa: BLE001 - 自进化召回失败不影响主链路
            logger.warning(f"自进化条目召回失败，按空结果继续：{exc}")
            evolution_chunks = []

    return {
        "embedding_chunks": embedding_chunks,
        "evolution_chunks": evolution_chunks,
    }
