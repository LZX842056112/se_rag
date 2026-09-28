"""查询图节点：MCP 联网检索（可选增强，失败降级为空结果）。"""
from app.rag.query.web_search_service import search_by_web
from app.shared.runtime.logger import node_log
from app.shared.utils.task_state import track_node_task


@node_log("node_web_search_mcp")
@track_node_task("node_web_search_mcp")
def node_web_search_mcp(state):
    """调用外部搜索引擎补充实时信息。"""
    return {"web_search_docs": search_by_web(state)}
