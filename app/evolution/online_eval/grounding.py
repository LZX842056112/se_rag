"""在线接地性（groundedness）评估：对答案相对证据片段做支持度打分。

走轻量 LLM 结构化输出；异常时安全降级为 0（表示「无法判定」），不阻断主链路。
"""
from __future__ import annotations

from langchain_core.messages import HumanMessage

from app.shared.models import llm_providers
from app.shared.runtime.logger import logger
from app.shared.utils.json_utils import parse_json_object

_PROMPT = (
    "你是接地性评估器。判断‘答案’的每一句是否被给定的‘证据片段’直接支持。\n"
    # JSON 结构示例中的花括号必须双写转义，否则会被 .format() 当作占位符解析
    "仅输出 JSON，禁止多余文字，格式 {{\"groundedness\": float(0~1), \"unsupported\": [\"句子\"]}}。\n"
    "证据片段：\n{context}\n\n答案：\n{answer}"
)
_EVIDENCE_CHAR_LIMIT = 500
_ANSWER_CHAR_LIMIT = 4000


def compute_groundedness(answer: str, evidence_texts: list[str]) -> float:
    """计算答案相对证据的接地分（0~1）；空证据或评估失败返回 0。"""
    if not answer or not evidence_texts:
        return 0.0
    try:
        context = "\n".join(f"- {text[:_EVIDENCE_CHAR_LIMIT]}" for text in evidence_texts)
        prompt = _PROMPT.format(context=context, answer=answer[:_ANSWER_CHAR_LIMIT])
        response = llm_providers.chat(json_mode=True).invoke([HumanMessage(content=prompt)])
        parsed = parse_json_object(response.content)
        return max(0.0, min(1.0, float(parsed.get("groundedness", 0.0))))
    except Exception as exc:  # noqa: BLE001 - 评估失败不影响主链路
        logger.warning(f"groundedness 评估失败，降级为 0：{exc}")
        return 0.0
