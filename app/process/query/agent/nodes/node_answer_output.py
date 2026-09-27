from app.shared.runtime.logger import node_log
from app.rag.query.answer_service import generate_answer
from app.shared.utils.task_utils import track_node_task

@node_log("node_answer_output")
@track_node_task("node_answer_output")
def node_answer_output(state):
    """
    节点功能：生成最终回答并交付给用户（支持流式/非流式）。
    """
    return generate_answer(state)