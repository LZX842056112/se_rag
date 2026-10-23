from typing_extensions import TypedDict
from typing import List
from app.rag.query.contracts import Intent
import copy

class QueryGraphState(TypedDict):
    """
    QueryGraphState 定义了整个查询流程中流转的数据结构。
    TypedDict 让我们在代码中能有自动补全和类型检查。
    使用字典式访问（如 state["session_id"]、state.get("answer")）。
    """
    session_id: str  # 会话唯一标识
    original_query: str  # 用户原始问题
    # 点选主体时前端附带的意图原句：用户点选前问过的那句话。
    # 非空时，主体消解直接用它拼出完整问句，不再让模型推断意图（避免被历史对话改写）。
    rewrite_hint: str

    # 意图识别结果："rag"=知识库问答（默认）/"chitchat"=闲聊；
    # 由图入口 node_intent_route 回填，决定走检索链路还是闲聊作答分支。
    intent: Intent
    turn_id: str
    answer_id: str
    business_tag: str
    handoff_required: bool
    issue_type: str
    issue_id: str
    metrics: dict
    agentic_trace: list
    evidence_manifest: list
    agentic_metrics: dict
    agentic_stop_reason: str
    agentic_verified: bool
    query_mode: str
    agentic_revalidation: list
    agentic_diagnostics: dict

    # 检索过程中的中间数据
    embedding_chunks: list  # 普通向量检索回来的切片
    hyde_embedding_chunks: list  # HyDE 检索回来的切片
    web_search_docs: list  # 网络搜索回来的文档

    # 支持额外检索源 (自进化召回) 的混合检索结果
    evolution_chunks: list  # 进化条目召回（kb_evolution_items）

    # 排序过程中的数据
    rrf_chunks: list  # RRF 融合排序后的切片
    reranked_docs: list  # 重排序后的最终 Top-K 文档

    # 生成过程中的数据
    prompt: str  # 组装好的 Prompt
    answer: str  # 最终生成的答案
    answer_contexts: list[str]  # 实际送入答案提示词的证据；不属于公开 API

    # 自进化辅助输出
    cited_chunk_ids: list  # 被采纳的引用 id（chunk_id / evo_doc_id）
    groundedness: float | None  # 答案接地性 0~1
    retrieval_signals: dict  # 零命中 / 无检索直达 / 进化命中 等信号
    faq_evo_ids: list  # 命中的自进化条目 id 列表
    citations: list  # 对外引用列表 [{faq_id, source}]，供落库与历史回显
    item_name_options: list  # 没确认到主体时的相似主体选项 [{item_name, score, matched_by}]

    # 辅助信息
    item_names: List[str]  # 提取出的商品名称
    rewritten_query: str  # 改写后的问题
    is_stream: bool  # 是否流式输出标记
    image_urls: List[str]  # 答案中引用的图片链接


# ========================
# 默认状态（全部为空）
# ========================
query_graph_default_state: QueryGraphState = {
    "session_id": "",
    "original_query": "",
    "rewrite_hint": "",
    "intent": "rag",
    "turn_id": "",
    "answer_id": "",
    "business_tag": "",
    "handoff_required": False,
    "issue_type": "",
    "issue_id": "",
    "agentic_trace": [],
    "evidence_manifest": [],
    "agentic_metrics": {},
    "agentic_stop_reason": "",
    "agentic_verified": False,
    "query_mode": "legacy",
    "agentic_revalidation": [],
    "agentic_diagnostics": {},
    "embedding_chunks": [],
    "hyde_embedding_chunks": [],
    "web_search_docs": [],
    "evolution_chunks": [],
    "rrf_chunks": [],
    "reranked_docs": [],
    "prompt": "",
    "answer": "",
    "answer_contexts": [],
    "cited_chunk_ids": [],
    "groundedness": None,
    "retrieval_signals": {},
    "faq_evo_ids": [],
    "citations": [],
    "item_name_options": [],
    "item_names": [],
    "rewritten_query": "",
    "is_stream": False,
    "image_urls": []
}


# ========================
# 创建默认状态（可覆盖）
# ========================
def create_query_default_state(**overrides) -> QueryGraphState:
    """
    创建查询流程的默认状态，支持覆盖字段
    """
    state = copy.deepcopy(query_graph_default_state)
    state.update(overrides)
    return state


# ========================
# 获取干净状态
# ========================
def get_query_default_state() -> QueryGraphState:
    """
    返回一个新的状态实例，避免全局变量污染。
    """
    return copy.deepcopy(query_graph_default_state)
