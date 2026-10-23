"""查询流程图：意图识别 → 闲聊分流 / 主体确认 → 三路召回（向量 / HyDE / 联网）→ RRF 融合 → 重排 → 父块回溯 → 作答。"""
from langgraph.graph import END, StateGraph

from app.process.query.agent.nodes.node_answer_output import node_answer_output
from app.process.query.agent.nodes.node_chitchat_answer import node_chitchat_answer
from app.process.query.agent.nodes.node_intent_route import node_intent_route
from app.process.query.agent.nodes.node_item_name_confirm import node_item_name_confirm
from app.process.query.agent.nodes.node_parent_expand import node_parent_expand
from app.process.query.agent.nodes.node_rerank import node_rerank
from app.process.query.agent.nodes.node_rrf import node_rrf
from app.process.query.agent.nodes.node_search_embedding import node_search_embedding
from app.process.query.agent.nodes.node_search_embedding_hyde import node_search_embedding_hyde
from app.process.query.agent.nodes.node_web_search_mcp import node_web_search_mcp
from app.process.query.agent.state import QueryGraphState
from app.shared.runtime.logger import logger
from app.shared.config import settings
from app.rag.agentic.budget import selected_mode


def node_agentic(state):
    from app.rag.agentic.controller import run_agentic
    state['query_mode'] = 'agentic'
    return run_agentic(state)

query_graph_builder = StateGraph(QueryGraphState)

# 1. 注册节点（意图路由与闲聊作答为本功能新增，其余为原有检索/作答链路）
query_graph_builder.add_node(node_intent_route)
query_graph_builder.add_node(node_chitchat_answer)
query_graph_builder.add_node(node_item_name_confirm)
query_graph_builder.add_node(node_search_embedding)
query_graph_builder.add_node(node_search_embedding_hyde)
query_graph_builder.add_node(node_web_search_mcp)
query_graph_builder.add_node(node_rrf)
query_graph_builder.add_node(node_rerank)
query_graph_builder.add_node(node_parent_expand)
query_graph_builder.add_node(node_answer_output)
query_graph_builder.add_node(node_agentic)

# 2. 入口节点：意图识别
query_graph_builder.set_entry_point("node_intent_route")


def after_node_intent_route(state: QueryGraphState):
    """路由：闲聊 → 闲聊作答；其余（rag / 开关关闭 / 识别失败兜底）→ 主体确认。"""
    if state.get("intent") == "chitchat":
        logger.info("意图识别为闲聊，进入闲聊作答节点")
        return "node_chitchat_answer"
    logger.info("意图识别为知识库问答（或闲聊开关关闭/兜底），进入主体确认")
    return "node_item_name_confirm"


query_graph_builder.add_conditional_edges("node_intent_route", after_node_intent_route, {
    "node_chitchat_answer": "node_chitchat_answer",
    "node_item_name_confirm": "node_item_name_confirm",
})


def after_node_item_name_confirm(state: QueryGraphState):
    """路由：主体已确认则并行三路召回；未确认（state.answer 已就绪）直接作答。"""
    if not state.get("answer"):
        if selected_mode(state.get('session_id', ''), mode=settings.agentic.mode,
                         percent=settings.agentic.gray_percent) == 'agentic':
            return 'node_agentic'
        logger.info(f"主体已确认：{state.get('item_names')}，进入多路召回")
        return "node_search_embedding", "node_search_embedding_hyde", "node_web_search_mcp"
    logger.info("主体未确认，直接进入作答节点（反问/兜底话术）")
    return "node_answer_output"


query_graph_builder.add_conditional_edges("node_item_name_confirm", after_node_item_name_confirm, {
    "node_search_embedding": "node_search_embedding",
    "node_search_embedding_hyde": "node_search_embedding_hyde",
    "node_web_search_mcp": "node_web_search_mcp",
    "node_answer_output": "node_answer_output",
    "node_agentic": "node_agentic",
})

# 3. 静态边
query_graph_builder.add_edge("node_search_embedding", "node_rrf")
query_graph_builder.add_edge("node_search_embedding_hyde", "node_rrf")
query_graph_builder.add_edge("node_web_search_mcp", "node_rrf")
query_graph_builder.add_edge("node_rrf", "node_rerank")
query_graph_builder.add_edge("node_rerank", "node_parent_expand")
query_graph_builder.add_edge("node_parent_expand", "node_answer_output")
# 闲聊分支：作答后直接结束，不经过检索链路任何节点
query_graph_builder.add_edge("node_chitchat_answer", END)
query_graph_builder.add_edge("node_answer_output", END)
query_graph_builder.add_edge("node_agentic", END)

query_app = query_graph_builder.compile()
