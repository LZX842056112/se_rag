"""
历史对话上下文工具。

商品名消解（item_name_confirm）与答案生成（answer_service）都需要把"近期有效
历史记录"拼成提示词片段。两处拼接规则一致，故抽到本模块统一维护。
"""
from app.infra.persistence.history_repository import history_repository
from app.shared.runtime.logger import logger


def list_valid_history(session_id: str, limit: int) -> list[dict]:
    """
    取出近期历史记录，并过滤出"带有效商品名"（item_names 非空）的消息。

    只保留与具体商品相关的历史，才能为后续主体消解提供可靠上下文。

    :param session_id: 会话 ID
    :param limit: 最近多少条
    :return: 有效历史消息列表（可能有空）
    """
    message_list = history_repository.list_recent(session_id=session_id, limit=limit)
    return [item for item in message_list if len(item.get("item_names", [])) > 0]


def build_history_context(session_id: str, limit: int) -> str:
    """
    获取近期"有效"历史记录并拼接为提示词上下文片段。

    用户消息取 rewritten_query，助手消息取 answer 前 50 字，均附带关联商品名，
    供模型做主体提取与问题重写。

    :param session_id: 会话 ID
    :param limit: 最多取多少条历史
    :return: 拼接后的提示词片段；无有效记录时返回固定提示串
    """
    final_message_list = list_valid_history(session_id, limit)
    if not final_message_list:
        logger.warning(f"当前会话:{session_id}没有有效的历史对话记录,history_text为空!")
        return "无有效对话记录!"
    history_text = ""
    for index, item in enumerate(final_message_list, start=1):
        role_prefix = "提问:" if item.get("role") == "user" else "回答:"
        content = (
            item.get("rewritten_query")
            if item.get("role") == "user"
            else item.get("text", "")[:50]
        )
        history_text += (
            f"序号:{index},{role_prefix}{content},"
            f"关联主体: {','.join(item.get('item_names'))} \n"
        )
    return history_text