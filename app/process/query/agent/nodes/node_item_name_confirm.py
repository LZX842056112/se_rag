"""查询图节点：主体确认（识别问题中的商品名并改写问题）。"""
from app.rag.query.item_name_confirm_service import confirm_item_name
from app.shared.runtime.logger import node_log
from app.shared.utils.task_state import track_node_task


@node_log("node_item_name_confirm")
@track_node_task("node_item_name_confirm")
def node_item_name_confirm(state):
    """输入 ``state['original_query']``，输出 ``state['item_names']`` / ``rewritten_query``。"""
    return confirm_item_name(state)
