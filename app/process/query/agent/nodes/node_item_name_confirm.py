from app.shared.runtime.logger import node_log
from app.rag.query.item_name_confirm_service import confirm_item_name
from app.shared.utils.task_utils import track_node_task

@node_log("node_item_name_confirm")
@track_node_task("node_item_name_confirm")
def node_item_name_confirm(state):
    """
    节点功能：确认用户问题中的核心商品名称。
    输入：state['original_query']
    输出：更新 state['item_names']
    """
    # 调用 rag/query service 层
    return confirm_item_name(state)


if __name__ == "__main__":
    mock_state = {
        "session_id": "test_session_001",
        "original_query": "他怎么用？",
        "is_stream": False,
    }
    result_state = node_item_name_confirm(mock_state)
    print(result_state)