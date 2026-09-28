"""查询图节点：生成最终回答（支持流式/非流式）。"""
from app.rag.query.answer_service import generate_answer
from app.shared.runtime.logger import node_log
from app.shared.utils.task_state import track_node_task


@node_log("node_answer_output")
@track_node_task("node_answer_output")
def node_answer_output(state):
    """生成答案、回填引用与接地性，并落库会话历史。"""
    return generate_answer(state)
