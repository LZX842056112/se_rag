"""确定性识别纯寒暄。只匹配完整输入，不匹配产品问句中的问候词。"""
from __future__ import annotations

import re

_SEPARATORS = re.compile(r"[\s!?！？。,.，、~～…]+")
_GREETINGS = frozenset({
    "你好", "您好", "你们好", "你好呀", "你好啊", "您好呀", "嗨", "哈喽",
    "在吗", "有人吗", "早上好", "中午好", "下午好", "晚上好", "早安", "晚安",
    "谢谢", "谢谢你", "谢谢您", "多谢", "辛苦了", "不客气", "再见", "拜拜",
    "hi", "hello", "hey", "thanks", "thankyou", "goodmorning",
})


def is_plain_greeting(query: str) -> bool:
    return _SEPARATORS.sub("", str(query or "")).casefold() in _GREETINGS
