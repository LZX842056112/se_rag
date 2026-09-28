"""「无法作答」兜底话术判定（查询端与自进化端共用一份口径）。

两处用途必须一致，否则会出现错配：
- 查询端据此决定「不渲染检索到的图片」（避免「说答不出、界面却给出图」的自相矛盾）；
- 自进化端据此把该回答记为未解决信号（缺口来源）。
"""
from __future__ import annotations

# 兜底话术标记：模型在「四类素材均无匹配有效信息」时输出的固定话术
NO_ANSWER_MARKERS = ("未查询到该问题相关信息", "无法作答")


def is_no_answer(answer: object) -> bool:
    """判断答案是否为「无法作答」型兜底话术。"""
    text = str(answer or "")
    return any(marker in text for marker in NO_ANSWER_MARKERS)
