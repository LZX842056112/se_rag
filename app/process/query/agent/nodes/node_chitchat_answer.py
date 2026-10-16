"""查询图节点：闲聊作答（意图识别为 chitchat 时的分支末节点）。"""
from app.rag.query.chitchat_service import generate_chitchat_answer
from app.shared.runtime.logger import node_log
from app.shared.utils.task_state import track_node_task


@node_log("node_chitchat_answer")
@track_node_task("node_chitchat_answer")
def node_chitchat_answer(state):
    """生成闲聊回复、回填 state 并落库会话历史。"""
    return generate_chitchat_answer(state)
