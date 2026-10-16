"""历史对话上下文工具。

主体名消解（``item_name_confirm_service``）与答案生成（``answer_service``）都需要把
「近期有效历史记录」拼成提示词片段，两处规则一致，故集中维护。

闲聊链路（意图分类与闲聊作答）另用 ``build_chat_history_context``：不过滤主体名，
否则闲聊消息（item_names 为空）会被 ``list_valid_history`` 滤掉，多轮闲聊丢失上文。
"""
from __future__ import annotations

from app.shared.clients.history_repository import history_repository
from app.shared.runtime.logger import logger


def list_valid_history(session_id: str, limit: int) -> list[dict]:
    """取出近期历史记录，并过滤出「带有效主体名」的消息。

    只保留与具体商品相关的历史，才能为后续主体消解提供可靠上下文。
    """
    message_list = history_repository.list_recent(session_id=session_id, limit=limit)
    return [item for item in message_list if len(item.get("item_names") or []) > 0]


def build_history_context(session_id: str, limit: int) -> str:
    """把近期「有效」历史拼成提示词上下文片段；无有效记录时返回固定提示串。"""
    final_message_list = list_valid_history(session_id, limit)
    if not final_message_list:
        logger.warning(f"当前会话 {session_id} 没有有效的历史对话记录，history_text 为空")
        return "无有效对话记录!"
    lines = []
    # list_recent 返回新 → 旧；反转为旧 → 新后编号，让 LLM 看到自然因果时间线
    # （序号 1 = 最早一条）。GET /api/history 的对外语义仍是新 → 旧，仓储排序不动。
    for index, item in enumerate(reversed(final_message_list), start=1):
        role_prefix = "提问:" if item.get("role") == "user" else "回答:"
        content = (
            item.get("rewritten_query")
            if item.get("role") == "user"
            else (item.get("text") or "")[:50]
        )
        item_names = ",".join(item.get("item_names") or [])
        lines.append(f"序号:{index},{role_prefix}{content},关联主体: {item_names}")
    return "\n".join(lines) + "\n"


def build_chat_history_context(session_id: str, limit: int) -> str:
    """把近期全部历史（含闲聊轮）拼成提示词片段；无历史返回固定提示串。

    与 ``build_history_context`` 的差别：不做「带有效主体名」过滤——闲聊消息
    ``item_names`` 为空，若过滤则多轮闲聊看不到上文。意图分类（规则「历史在讨论
    产品、本轮跟进追问 → rag」）与闲聊作答（保持话题连贯）共用本函数。
    """
    message_list = history_repository.list_recent(session_id=session_id, limit=limit)
    if not message_list:
        logger.warning(f"当前会话 {session_id} 没有历史对话记录，history_text 为空")
        return "无对话记录!"
    lines = []
    # 同 build_history_context 口径：list_recent 返回新 → 旧，反转为旧 → 新后编号
    for index, item in enumerate(reversed(message_list), start=1):
        role_prefix = "用户:" if item.get("role") == "user" else "助手:"
        content = item.get("text") or ""
        lines.append(f"序号:{index},{role_prefix}{content}")
    return "\n".join(lines) + "\n"
