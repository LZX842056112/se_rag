"""查询图节点：父块回溯（把章节背景注入重排结果）。"""
from app.rag.query.parent_expand_service import expand_docs_with_parents
from app.shared.runtime.logger import node_log
from app.shared.utils.task_state import track_node_task


@node_log("node_parent_expand")
@track_node_task("node_parent_expand")
def node_parent_expand(state):
    """为命中的本地子块回填所属章节背景，供作答节点拼装上下文。"""
    state["reranked_docs"] = expand_docs_with_parents(state.get("reranked_docs") or [])
    return state
