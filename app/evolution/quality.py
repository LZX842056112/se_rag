"""候选知识质量判定：把「无信息」型答案挡在知识库之外。

背景（真实事故）：缺口候选在**没有检索证据**时，大模型会把「文档里没有 / 建议联系官方」
写成 FAQ 答案；管理员审批后它作为「知识」入库，客服提问时虽然能召回（引用标签显示
「自进化」），但答案模型读到的是“未说明、去问官方”，于是仍然回答“无法作答”。

结论：候选必须携带事实。凡是疑似「无信息」结论的答案，一律不给 ``draft``，而是登记为
``need_info``（待人工补充），由管理员补充真实答案后再通过。
"""
from __future__ import annotations

import re
from typing import Sequence

# 「无信息」结论的常见表述（中文语料为主，兼容少量英文）
_NON_ANSWER_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"未提及",
        r"未说明",
        r"未包含",
        r"未提供",
        r"未找到",
        r"没有(?:提及|说明|提供|找到|相关)",
        r"无法(?:确定|作答|回答|判断)",
        r"未查询到",
        r"资料中无",
        r"文档中无",
        r"信息缺失",
        r"建议(?:查阅|咨询|联系)",
        r"请(?:查阅|咨询|联系)",
        r"not\s+(?:mentioned|specified|provided|found)",
        r"insufficient\s+information",
    )
)

# 答案短于该长度基本不可能承载事实
_MIN_ANSWER_CHARS = 8


def looks_like_non_answer(answer: str | None, *, patterns: Sequence[re.Pattern[str]] | None = None) -> bool:
    """判断答案是否为「无信息 / 无法作答」型（不应作为知识入库）。

    :param answer: 候选答案正文
    :param patterns: 可覆盖的匹配规则（测试用）
    """
    text = str(answer or "").strip()
    if not text or len(text) < _MIN_ANSWER_CHARS:
        return True
    for pattern in (patterns if patterns is not None else _NON_ANSWER_PATTERNS):
        if pattern.search(text):
            return True
    return False
