"""查询图节点：HyDE 假设性文档检索。"""
from app.rag.query.hyde_search_service import search_by_hyde
from app.shared.runtime.logger import node_log
from app.shared.utils.task_state import track_node_task


@node_log("node_search_embedding_hyde")
@track_node_task("node_search_embedding_hyde")
def node_search_embedding_hyde(state):
    """先让 LLM 生成假设性答案，再用「问题 + 假设答案」检索，提升召回率。"""
    return {"hyde_embedding_chunks": search_by_hyde(state)}
