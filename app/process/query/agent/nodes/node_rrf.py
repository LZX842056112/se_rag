from app.shared.runtime.logger import node_log
from app.rag.query.rrf_service import fuse_by_rrf
from app.shared.utils.task_utils import track_node_task

@node_log("node_rrf")
@track_node_task("node_rrf")
def node_rrf(state):
    """
    节点功能：Reciprocal Rank Fusion
    将多路召回的结果（向量、HyDE、Web）进行加权融合排序。
    """
    return fuse_by_rrf(state)