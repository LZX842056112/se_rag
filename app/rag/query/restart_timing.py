"""重启生效时间只能由适用文档中的原句确认。"""
from __future__ import annotations

import re

from app.shared.utils.answer import NO_ANSWER_REPLY
from app.shared.utils.text import is_same_entity, split_subjects

_RESTART = r"(?:重启|重新启动|重新开机)"
_QUESTION = re.compile(
    rf"{_RESTART}(?:之后|以后|后)[^。；;！？!?\n，,]{{0,20}}"
    r"(?:立即|即时|立刻|马上|即刻|何时|什么时候|多久|多长时间)[^。；;！？!?\n，,]{0,10}生效(?:吗|么)?$"
)
_OPERATION = re.compile(rf"(?:点击|单击|按下)[^。！？\n]{{0,12}}{_RESTART}[^。！？\n]{{0,12}}按钮[^。！？\n]*{_RESTART}")
_TIMING = re.compile(r"立即|即时|立刻|马上|即刻|随即|延迟|等待|无需|无须|\d+\s*(?:秒|分钟|小时)")
_IMAGE_OR_LINK = re.compile(r"!?\[[^\]\n]*\]\([^\n)]*\)")
_SENTENCE = re.compile(r"[^。！？!?\n|]+[。！？!?]?")
_RESTART_WORD = re.compile(_RESTART)
_UNKNOWN = "现有资料没有说明重启后何时生效。"

QUOTE_SCHEMA = '{"operation_quote":"文档原句或空字符串","effect_quote":"文档原句或空字符串"}'
QUOTE_INSTRUCTIONS = (
    f"本题只返回 JSON：{QUOTE_SCHEMA}。"
    "operation_quote 复制说明重启操作的完整正文原句。"
    "effect_quote 复制明确说明同一设置重启后何时生效的完整正文原句。"
    "检查型号和设置。用户未指定设置时，不使用某一项专用设置的规则。"
    "保留条件和否定词。不要用图片描述、标题或历史对话作为依据。"
    "缺少原句时留空。点击按钮会重启，不代表立即重启或设置立即生效。"
)


def _question(state: dict) -> str:
    return state.get("rewrite_hint") or state.get("original_query") or ""


def needs_restart_timing_quotes(state: dict) -> bool:
    question = _question(state).strip().rstrip("。！？!?")
    return (state.get("business_tag") == "product_knowledge" and len(state.get("item_names") or ()) == 1
            and not re.search(r"[。！？!?；;\n，,]", question) and _QUESTION.search(question) is not None)


def _normalize(sentence: str) -> str:
    # PDF 表格可能在汉字中间插入空白。只忽略这类排版差异。
    return re.sub(r"(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])", "", sentence.strip(" \t#*"))


def _sentences(state: dict) -> set[str]:
    product, = state["item_names"]
    sentences: set[str] = set()
    for doc in state.get("reranked_docs", ()):
        if doc.get("type") == "web" or doc.get("source") == "web":
            continue  # 没有经过主体确认的网页不用于确认时效。
        if not any(is_same_entity(product, name) for name in split_subjects(doc.get("item_name"))):
            continue
        for body in (doc.get("text"), doc.get("parent_content")):
            if body:
                sentences.update(_normalize(s) for s in _SENTENCE.findall(_IMAGE_OR_LINK.sub("\n", body)))
    return sentences


def restart_timing_has_explicit_effect(state: dict) -> bool:
    return any(_is_effect(sentence) and _TIMING.search(sentence) for sentence in _sentences(state))


def _is_effect(text: str) -> bool:
    return "生效" in text and _RESTART_WORD.search(text) is not None


def render_restart_timing_answer(state: dict, quotes: dict) -> str:
    """展示可逐句核对的正文。保留证据中的前提、否定词和时效。"""
    sentences = _sentences(state)

    def verified(key: str) -> str:
        value = quotes.get(key)
        normalized = _normalize(value) if isinstance(value, str) else ""
        return normalized if normalized in sentences else ""

    effect = verified("effect_quote")
    if not _is_effect(effect):
        effect = ""
    if effect and _TIMING.search(effect):
        return effect
    operation = verified("operation_quote")
    if not _OPERATION.search(operation):
        # PDF 里同一句可能重复出现。只有一个明确操作时才直接采用。
        choices = {sentence for sentence in sentences if _OPERATION.search(sentence)}
        if len(choices) == 1:
            operation, = choices
        else:
            operation = ""
    evidence = effect or operation
    if not evidence:
        return NO_ANSWER_REPLY
    readable = re.sub(r"<([^<>]+)>", r"“\1”", evidence)
    return readable.rstrip("。！？!?；;") + "。" + _UNKNOWN
