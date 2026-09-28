"""查询图节点：RRF 多路召回融合。"""
from app.rag.query.rrf_service import fuse_by_rrf
from app.shared.runtime.logger import node_log
from app.shared.utils.task_state import track_node_task


@node_log("node_rrf")
@track_node_task("node_rrf")
def node_rrf(state):
    """把向量、HyDE、联网与自进化结果按 RRF 融合排序。"""
    return fuse_by_rrf(state)
