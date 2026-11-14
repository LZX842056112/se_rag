"""Definitions for the workflow graphs sent to the monitor."""
from functools import lru_cache

from app.shared.runtime.workflow_monitor import CANDIDATE, CYCLE, IMPORT, QUERY

_LABELS = {
    "__start__": "开始", "__end__": "结束", "upload_file": "保存文件",
    "node_entry": "检查文件", "node_pdf_to_md": "转换 PDF", "node_md_img": "处理图片",
    "node_document_split": "切分文档", "node_item_name_recognition": "识别主体",
    "node_bge_embedding": "生成向量", "node_import_milvus": "写入知识库",
    "node_intent_route": "识别意图", "node_chitchat_answer": "生成闲聊答复",
    "node_item_name_confirm": "确认主体", "node_search_embedding": "检索本地知识库",
    "node_search_embedding_hyde": "按假设答案检索", "node_web_search_mcp": "搜索网络",
    "node_rrf": "合并检索结果", "node_rerank": "重排结果", "node_parent_expand": "读取完整章节",
    "node_answer_output": "生成答案", "node_agentic": "Agentic 问答",
    "agentic:__start__": "开始 Agentic 问答", "agentic:__end__": "结束 Agentic 问答",
    "bootstrap": "检索初始证据", "assess": "评估证据", "select": "选择操作",
    "execute": "执行操作", "draft": "生成草稿", "verify": "核验草稿",
    "publication": "处理发布队列", "scan": "扫描反馈", "gaps": "处理缺口",
    "metrics": "记录指标", "tuning": "调整参数", "backtest": "执行回测",
    "feedback": "登记反馈", "gap": "判定缺口", "strategy": "处理策略待办",
    "candidate": "登记候选", "supplement": "补充资料", "approval": "审批候选",
    "publish": "发布到知识库", "reject": "驳回候选", "withdraw": "下架候选",
}

_CYCLE_NODES = ("__start__", "publication", "scan", "gaps", "metrics", "tuning", "backtest", "__end__")
_CANDIDATE_NODES = ("feedback", "gap", "strategy", "candidate", "supplement", "approval", "publish", "reject", "withdraw", "__end__")


def _edge(source: str, target: str, conditional: bool = False) -> dict:
    return {"source": source, "target": target, "conditional": conditional}


def _compiled_graph(app, prefix: str = "") -> tuple[list[str], list[dict]]:
    graph = app.get_graph()
    return (
        [prefix + key for key in graph.nodes],
        [_edge(prefix + edge.source, prefix + edge.target, edge.conditional) for edge in graph.edges],
    )


@lru_cache(maxsize=4)
def workflow_graph(view: str) -> dict:
    if view == IMPORT:
        from app.process.import_.agent.main_graph import import_app

        nodes, edges = _compiled_graph(import_app)
        nodes.insert(1, "upload_file")
        edges = [_edge("__start__", "upload_file"), *[
            {**edge, "source": "upload_file"} if edge["source"] == "__start__" else edge for edge in edges
        ]]
    elif view == QUERY:
        from app.process.query.agent.main_graph import query_app
        from app.rag.agentic.controller import _graph

        nodes, edges = _compiled_graph(query_app)
        inner_nodes, inner_edges = _compiled_graph(_graph, "agentic:")
        nodes.extend(["node_web_search_mcp", *inner_nodes])
        edges.extend([
            _edge("node_search_embedding", "node_web_search_mcp", True),
            _edge("node_web_search_mcp", "node_rrf"),
            _edge("node_agentic", "agentic:__start__", True),
            _edge("agentic:__end__", "__end__"),
            *inner_edges,
        ])
    elif view == CYCLE:
        nodes = _CYCLE_NODES
        edges = [_edge(source, target) for source, target in zip(nodes, nodes[1:])]
    elif view == CANDIDATE:
        nodes = _CANDIDATE_NODES
        edges = [_edge("feedback", "gap"), _edge("gap", "__end__", True),
                 _edge("gap", "strategy", True), _edge("strategy", "__end__", True),
                 _edge("strategy", "candidate", True), _edge("gap", "candidate", True),
                 _edge("candidate", "supplement", True), _edge("candidate", "approval", True),
                 _edge("supplement", "approval"), _edge("approval", "publish", True),
                 _edge("candidate", "reject", True), _edge("approval", "reject", True),
                 _edge("publish", "publish", True), _edge("publish", "__end__"),
                 _edge("publish", "withdraw", True), _edge("withdraw", "__end__"),
                 _edge("reject", "withdraw", True),
                 _edge("reject", "__end__")]
    else:
        raise ValueError("Unknown workflow view")
    return {"nodes": [{"id": node, "label": _LABELS.get(node) or _LABELS.get(node.removeprefix("agentic:"), node)} for node in nodes],
            "edges": edges}


def legacy_nodes(view: str, done: list, running: list) -> dict:
    """Translate saved display names from older import and query snapshots."""
    from app.shared.utils.task_state import _NODE_NAME_TO_CN

    reverse = {label: node for node, label in _NODE_NAME_TO_CN.items() if node != "END"}
    ids = {node["id"] for node in workflow_graph(view)["nodes"]}
    states = {reverse.get(label, label): "completed" for label in done}
    states.update({reverse.get(label, label): "running" for label in running})
    return {node_id: {"status": status, "visits": 1} for node_id, status in states.items() if node_id in ids}
