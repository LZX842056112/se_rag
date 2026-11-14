"""「无法作答」兜底话术判定（查询端与自进化端共用一份口径）。

两处用途必须一致，否则会出现错配：
- 查询端据此决定「不渲染检索到的图片」（避免「说答不出、界面却给出图」的自相矛盾）；
- 自进化端据此把该回答记为未解决信号（缺口来源）。
"""
from __future__ import annotations

import re

NO_ANSWER_REPLY = "现有参考内容与历史对话中未查询到该问题相关信息，无法作答"
_REFUSAL_PREFIX = re.compile(
    r"^(?:\s*[#>*_`]*\s*(?:回答|结论|答复|answer|conclusion|抱歉|很抱歉|非常抱歉|对不起)"
    r"[\s#>*_`，,：:]*)+", re.IGNORECASE,
)
_REFUSAL_ONLY = re.compile(
    r"^(?:(?:现有)?(?:参考内容(?:与历史对话)?|资料|文档)(?:中)?"
    r"(?:未提及|未提供|未查询到)(?:该问题相关信息|该信息|相关信息)[，,。\s]*)?"
    r"(?:未查询到该问题相关信息|无法作答)"
    r"[。！.!，,\s]*(?:(?:请|建议)(?:交由|联系|咨询|查阅)[^。！!?\n]+[。！.!]?)?$"
)


def is_no_answer(answer: object) -> bool:
    """判断整条答案是否为兜底话术。"""
    text = str(answer or "").strip(" \t\r\n#>*_`")
    body = _REFUSAL_PREFIX.sub("", text)
    return body.startswith(NO_ANSWER_REPLY) or _REFUSAL_ONLY.fullmatch(body) is not None
