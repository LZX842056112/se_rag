"""主体确认服务（查询链路首节点）。

职责：结合历史对话提取商品名 → 改写问题 → 在主体名集合中混合检索并打分 →
自动确认标准主体，或生成反问让用户确认；同时把本轮提问写入会话历史。
"""
from __future__ import annotations

from langchain_core.messages import HumanMessage

from app.process.query.agent.state import QueryGraphState
from app.rag.item_name.match import search_by_item_names, select_item_names
from app.rag.query.history_utils import build_history_context
from app.shared.clients.history_repository import history_repository
from app.shared.models import llm_providers
from app.shared.runtime.logger import logger, step_log
from app.shared.runtime.prompts import load_prompt
from app.shared.utils.json_utils import parse_json_object
from app.shared.utils.require import require_state_str


@step_log("get_history_messages_and_context")
def get_history_messages_and_context(session_id: str) -> str:
    """获取近期有效历史对话并拼接为提示词上下文片段。"""
    return build_history_context(session_id, limit=10)


@step_log("call_llm_item_name_and_rewritten")
def call_llm_item_name_and_rewritten(history_text: str, original_query: str) -> dict:
    """调用模型识别主体名并改写问题（历史上下文放在规则之后，避免干扰规则读取）。"""
    json_llm_client = llm_providers.chat(json_mode=True)
    history_prompt_text = load_prompt(
        "rewritten_query_and_itemnames", query=original_query, history_text=history_text
    )
    response = json_llm_client.invoke([HumanMessage(content=history_prompt_text)])
    result_dict = parse_json_object(response.content)
    # 参数校验赋予默认值：模型可能只返回其中一个字段
    result_dict.setdefault("item_names", [])
    if not result_dict.get("rewritten_query"):
        result_dict["rewritten_query"] = original_query
    return result_dict


@step_log("apply_item_name_result")
def apply_item_name_result(state: QueryGraphState, list_dict: dict[str, list], rewritten_query: str) -> None:
    """根据「确认列表 / 可选列表」改写 state 的 answer、item_names 与 rewritten_query。

    - 确认列表非空：正常进入多路召回（answer 置空）；
    - 可选列表非空：写入候选主体与改写问题并回问用户（候选主体落库后，下一轮才能消解）；
    - 均为空：提示用户补充主体后再提问，直接作答后结束本轮。
    """
    confirmed_list = list_dict.get("confirmed_list", [])
    option_list = list_dict.get("option_list", [])

    if confirmed_list:
        state["item_names"] = [item.get("item_name") for item in confirmed_list]
        state["rewritten_query"] = rewritten_query
        if "answer" in state:
            state["answer"] = None
        return

    if option_list:
        # 候选主体与改写问题一并写入本轮 state：随消息落库后，用户下一轮回「是 X」时才能被消解。
        # 注意：路由只判 state["answer"] 是否为空，写入 item_names 不改变本轮回问的路由结果。
        candidate_names: list[str] = []
        for item in option_list:
            name = item.get("item_name")
            if name and name not in candidate_names:
                candidate_names.append(name)
        if candidate_names:
            state["item_names"] = candidate_names
        if rewritten_query:
            state["rewritten_query"] = rewritten_query
        state["answer"] = f"本次提问没有确认主体,但是有相似可选的: {','.join(candidate_names)},请您再次确认!"
        return

    state["answer"] = "本次问题没有关联到任何主体,有没有相似可选的主体! 请您明确主体再提问!"


@step_log("save_history_message")
def save_history_message(state: QueryGraphState) -> None:
    """把本轮用户提问写入会话历史（供下一轮主体消解使用）。"""
    history_repository.save_message(
        session_id=state.get("session_id"),
        role="user",
        text=state.get("original_query"),
        rewritten_query=state.get("rewritten_query", ""),
        item_names=state.get("item_names", []),
        image_urls=[],
    )


@step_log("confirm_item_name")
def confirm_item_name(state: QueryGraphState) -> QueryGraphState:
    """主体确认服务入口：1) 历史上下文 → 2) LLM 提取主体与改写 → 3) 向量对齐 → 4) 回写 state。"""
    session_id = require_state_str(state, "session_id")
    original_query = require_state_str(state, "original_query")
    logger.info(f"主体确认开始：session_id={session_id}, query={original_query}")

    history_text = get_history_messages_and_context(session_id)
    result = call_llm_item_name_and_rewritten(history_text, original_query)

    list_dict: dict[str, list] = {}
    if result.get("item_names"):
        milvus_result = search_by_item_names(result["item_names"])
        list_dict = select_item_names(milvus_result)

    apply_item_name_result(state, list_dict, result.get("rewritten_query"))
    save_history_message(state)
    return state
