"""
候选内容 PII 脱敏与拦截：手机/身份证/单号/邮箱/地址→占位符。
命中则应被拦截，防止用户隐私写入知识库。
"""
from __future__ import annotations

import re

# 按顺序替换，先长后短避免误伤
_PATTERNS: list[tuple[str, str]] = [
    (r"\b1[3-9]\d{9}\b", "[手机号]"),                      # 大陆手机号
    (r"\b\d{6}[- ]?\d{8}[- ]?\d{1}\b", "[身份证]"),        # 身份证
    (r"\b\d{17}[\dXx]\b", "[身份证]"),
    (r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "[邮箱]"),
    (r"\bHK\w{4,}\b", "[主机编号]"),                       # 设备/主机编号类前缀
    (r"\b\d{6,}\b", "[单号]"),                             # 长数字串（单号/金额以外的编码）
]

EMAIL_OR_ID = re.compile(
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}|\b\d{17}[\dXx]\b|\b1[3-9]\d{9}\b"
)


def redact(text: str) -> str:
    """把正文中的 PII 替换为占位符。"""
    if not text:
        return text
    for pattern, repl in _PATTERNS:
        text = re.sub(pattern, repl, text)
    return text


def contains_pii(text: str) -> bool:
    """判断文本是否含敏感 PII。"""
    hits = EMAIL_OR_ID.findall(text or "")
    # 单号/长数字串按须脱敏，不直接判 PII（可能为产品型号）
    return bool(hits)


def sanitize_candidate(faq_question: str, faq_answer: str) -> tuple[str, str, bool]:
    """
    对候选 Q/A 做脱敏。
    返回 (脱敏后问题, 脱敏后答案, 是否含 PII 需拦截)。
    """
    has_pii = contains_pii(faq_question) or contains_pii(faq_answer)
    return redact(faq_question), redact(faq_answer), has_pii