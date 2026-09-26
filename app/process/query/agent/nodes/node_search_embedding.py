from app.shared.runtime.logger import node_log
from app.shared.utils.task_utils import track_node_task
from app.rag.query.embedding_search_service import search_by_embedding
from app.evolution.config import evolution_config
from app.evolution.retrieval import search_evolution_items

@node_log("node_search_embedding")
@track_node_task("node_search_embedding")
def node_search_embedding(state):
    """
    节点功能：进行向量内容检索（含自进化条目召回）。
    """
    embedding_chunks = search_by_embedding(state)
    evolution_chunks = []
    # 自进化召回：开启时并行查 kb_evolution_items；异常吞掉不影响主链路
    if getattr(evolution_config, "enabled", False):
        try:
            evolution_chunks = search_evolution_items(
                rewritten_query=state.get("rewritten_query", ""),
                item_names=state.get("item_names", []),
                limit=evolution_config.evolution_recall_limit,
            )
        except Exception:
            evolution_chunks = []
    return {
        "embedding_chunks": embedding_chunks,
        "evolution_chunks": evolution_chunks,
    }

if __name__ == "__main__":
    test_state = {
        "session_id": "test_search_embedding_001",
        "rewritten_query": "HAK 180 烫金机使用说明",
        "item_names": ["HAK 180 烫金机"],
        "is_stream": False,
    }
    result = node_search_embedding(test_state)
    print(result)