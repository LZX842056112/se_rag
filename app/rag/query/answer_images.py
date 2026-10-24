"""只发布答案明确选用、且能在本轮实际上下文中核验的图片。"""
from __future__ import annotations

import re
from html import unescape
from urllib.parse import urlsplit

from app.rag.config import SUPPORTED_IMAGE_EXTENSIONS
from app.shared.utils.answer import is_no_answer


_IMAGE_RE = re.compile(
    r"!\[[^\]\r\n]*\]\(\s*(?P<url><[^>\r\n]+>|(?:[^()\s]|\([^()\r\n]*\))+?)"
    r"(?:\s+[\"'][^\r\n]*?[\"'])?\s*\)"
)
_MARKER_RE = re.compile(r"^[ \t]*(?:【[ \t]*图片[ \t]*】|\[[ \t]*图片[ \t]*\])[ \t]*\r?$", re.MULTILINE)
_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


def _image_url(value: str) -> str:
    value = unescape(value.strip().strip("<>"))
    try:
        parsed = urlsplit(value)
        if (parsed.scheme in {"http", "https"} and parsed.netloc
                and parsed.path.lower().endswith(SUPPORTED_IMAGE_EXTENSIONS)):
            return value
    except ValueError:
        pass
    return ""


def _urls(text: str) -> list[str]:
    return [url for match in _URL_RE.finditer(text)
            if (url := _image_url(match.group().rstrip(")]}，。,;；】）＞")))]


def _split_image_section(answer: str) -> tuple[str, str]:
    """只识别独立标记行后、以纯链接或图片引用结尾的图片段。"""
    for marker in _MARKER_RE.finditer(answer):
        tail = answer[marker.end():].strip()
        lines = [re.sub(r"^(?:[-*+]\s+|\d+[.)]\s+)", "", line.strip())
                 for line in tail.splitlines() if line.strip()]
        if not lines or all(_URL_RE.fullmatch(line.strip("<>")) or _IMAGE_RE.fullmatch(line) for line in lines):
            return answer[:marker.start()], tail
    return answer, ""


def select_answer_images(state: dict) -> None:
    """校验后统一为正文加可选的图片段；不读取未送入模型的检索图片。"""
    answer = str(state.get("answer") or "")
    body, image_section = _split_image_section(answer)
    requested = [_image_url(match.group("url")) for match in _IMAGE_RE.finditer(body)]
    requested.extend(_urls(image_section))
    body = _IMAGE_RE.sub("", body).strip()

    allowed = set()
    for context in state.get("answer_contexts") or []:
        allowed.update(_urls(str(context)))
        allowed.update(_image_url(match.group("url")) for match in _IMAGE_RE.finditer(str(context)))
    images = []
    if not is_no_answer(body) and not state.get("handoff_required"):
        images = list(dict.fromkeys(url for url in requested if url and url in allowed))
    state["image_urls"] = images
    state["answer"] = body + ("\n\n【图片】\n" + "\n".join(images) if images else "")
