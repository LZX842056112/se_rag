"""查询流程图：主体确认 → 三路召回（向量 / HyDE / 联网）→ RRF 融合 → 重排 → 作答。"""
from langgraph.graph import END, StateGraph

from app.process.query.agent.nodes.node_answer_output import node_answer_output
from app.process.query.agent.nodes.node_item_name_confirm import node_item_name_confirm
from app.process.query.agent.nodes.node_rerank import node_rerank
from app.process.query.agent.nodes.node_rrf import node_rrf
from app.process.query.agent.nodes.node_search_embedding import node_search_embedding
from app.process.query.agent.nodes.node_search_embedding_hyde import node_search_embedding_hyde
from app.process.query.agent.nodes.node_web_search_mcp import node_web_search_mcp
from app.process.query.agent.state import QueryGraphState
from app.shared.runtime.logger import logger

query_graph_builder = StateGraph(QueryGraphState)

# 1. 注册节点
query_graph_builder.add_node(node_item_name_confirm)
query_graph_builder.add_node(node_search_embedding)
query_graph_builder.add_node(node_search_embedding_hyde)
query_graph_builder.add_node(node_web_search_mcp)
query_graph_builder.add_node(node_rrf)
query_graph_builder.add_node(node_rerank)
query_graph_builder.add_node(node_answer_output)

# 2. 入口节点
query_graph_builder.set_entry_point("node_item_name_confirm")


def after_node_item_name_confirm(state: QueryGraphState):
    """路由：主体已确认则并行三路召回；未确认（state.answer 已就绪）直接作答。"""
    if not state.get("answer"):
        logger.info(f"主体已确认：{state.get('item_names')}，进入多路召回")
        return "node_search_embedding", "node_search_embedding_hyde", "node_web_search_mcp"
    logger.info("主体未确认，直接进入作答节点（反问/兜底话术）")
    return "node_answer_output"


query_graph_builder.add_conditional_edges("node_item_name_confirm", after_node_item_name_confirm, {
    "node_search_embedding": "node_search_embedding",
    "node_search_embedding_hyde": "node_search_embedding_hyde",
    "node_web_search_mcp": "node_web_search_mcp",
    "node_answer_output": "node_answer_output",
})

# 3. 静态边
query_graph_builder.add_edge("node_search_embedding", "node_rrf")
query_graph_builder.add_edge("node_search_embedding_hyde", "node_rrf")
query_graph_builder.add_edge("node_web_search_mcp", "node_rrf")
query_graph_builder.add_edge("node_rrf", "node_rerank")
query_graph_builder.add_edge("node_rerank", "node_answer_output")
query_graph_builder.add_edge("node_answer_output", END)

query_app = query_graph_builder.compile()
