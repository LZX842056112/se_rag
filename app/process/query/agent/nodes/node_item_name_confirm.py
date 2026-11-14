"""查询图节点：主体确认（识别问题中的商品名并改写问题）。"""
from app.rag.agentic.budget import selected_mode
from app.rag.query.chunk_search import resolve_manual_heading
from app.rag.query.item_name_confirm_service import confirm_item_name
from app.shared.config import settings
from app.shared.runtime.logger import node_log
from app.shared.utils.task_state import track_node_task


@node_log("node_item_name_confirm")
@track_node_task("node_item_name_confirm")
def node_item_name_confirm(state):
    """输入 ``state['original_query']``，输出 ``state['item_names']`` / ``rewritten_query``。"""
    result = confirm_item_name(state)
    if result.get("item_names") and not result.get("answer") and selected_mode(
        result["session_id"], mode=settings.agentic.mode, percent=settings.agentic.gray_percent,
    ) == "legacy":
        result["requested_heading_path"] = resolve_manual_heading(
            result["item_names"], result.get("rewrite_hint") or result["original_query"],
        )
    return result
