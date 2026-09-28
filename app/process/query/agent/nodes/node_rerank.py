"""查询图节点：Cross-Encoder 精排。"""
from app.rag.query.rerank_service import rerank_documents
from app.shared.runtime.logger import node_log
from app.shared.utils.task_state import track_node_task


@node_log("node_rerank")
@track_node_task("node_rerank")
def node_rerank(state):
    """使用 BGE-Reranker 对 RRF 结果精确打分并动态截断。"""
    return rerank_documents(state)
