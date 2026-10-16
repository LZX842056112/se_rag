"""查询图节点：意图识别（rag / chitchat 分流）。"""
from app.rag.query.intent_route_service import route_intent
from app.shared.runtime.logger import node_log
from app.shared.utils.task_state import track_node_task


@node_log("node_intent_route")
@track_node_task("node_intent_route")
def node_intent_route(state):
    """判定本轮意图并回填 state['intent']，供条件边分流。"""
    return route_intent(state)
