"""
在线接地性（groundedness）评估：对答案相对证据片段做逐句支持度打分。
默认走轻量 LLM 结构化输出；异常时安全降级为 0（表示"无法判定"，不阻断主链路）。
"""
from __future__ import annotations

import json

from langchain_core.messages import HumanMessage

from app.infra.llm.providers import llm_providers
from app.shared.runtime.logger import logger

_PROMPT = (
    "你是接地性评估器。判断‘答案’的每一句是否被给定的‘证据片段’直接支持。\n"
    # 注意：JSON 结构示例里的花括号必须双写转义，否则会被下方 .format() 当作占位符解析
    "仅输出 JSON，禁止多余文字，格式 {{\"groundedness\": float(0~1), \"unsupported\": [\"句子\"]}}。\n"
    "证据片段：\n{context}\n\n答案：\n{answer}"
)


def compute_groundedness(answer: str, evidence_texts: list[str]) -> float:
    """
    计算 answer 相对 evidence_texts 的接地分（0~1）。
    空证据返回 0；评估失败返回 0。
    """
    if not answer or not evidence_texts:
        return 0.0
    try:
        context = "\n".join(f"- {t[:500]}" for t in evidence_texts)
        messages = [HumanMessage(content=_PROMPT.format(context=context, answer=answer[:4000]))]
        llm = llm_providers.chat(mode_name=None, json_mode=True)
        resp = llm.invoke(messages)
        text = resp.content.strip()
        # 剥掉代码围栏后解析 JSON
        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        parsed = json.loads(text)
        groundedness = float(parsed.get("groundedness", 0.0))
        return max(0.0, min(1.0, groundedness))
    except Exception as e:  # 评估失败不影响主链路
        logger.warning(f"groundedness 评估失败，降级为 0: {e}")
        return 0.0