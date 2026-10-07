"""MinerU ``content_list.json`` → 归一化 Block 流（结构感知解析路径）。

相对「从渲染后的 Markdown 反推结构」，直接消费结构化中间产物可以拿到三样 Markdown 里
拿不到或不可靠的信息：

1. ``type``——可**丢弃** ``header`` / ``footer`` / ``page_number`` 这类页眉页脚噪声
   （实测样本含 ``brother``、``© 2021 Brother Industries…``、页码 ``1``，此前会作为正文入库）；
2. ``table`` 的 ``table_body``——HTML 表格，可还原为 Markdown 表格并整表原子化，
   避免长表被切碎、丢失表头；
3. ``text_level`` 与 ``page_idx``——标题层级与页码，用于面包屑与引用溯源。

图片说明来自上游 ``enrich_markdown_images`` 已写入 Markdown 的视觉模型描述，本模块通过
「图片文件名 → 增强后的 Markdown 引用」映射回填，无需改动上游链路。
"""
from __future__ import annotations

import re
from pathlib import Path

from bs4 import BeautifulSoup

from app.rag.import_.section_splitter import Block

# MinerU content_list 中需要丢弃的块类型（页眉/页脚/页码等页面装饰）
_DROPPED_TYPES = {"header", "footer", "page_number", "page_footnote", "aside_text", "discarded"}

# Markdown 图片引用：![说明](地址)
_IMAGE_REF_RE = re.compile(r"!\[(.*?)\]\(([^)]*)\)")


def find_content_list(md_path: str | Path | None) -> Path | None:
    """在 Markdown 同目录下查找 MinerU 的 ``*_content_list.json``。

    只匹配 v1（``*_content_list.json``）——``*_content_list*.json`` 会同时命中结构不同的
    ``*_content_list_v2.json``。
    """
    if not md_path:
        return None
    directory = Path(md_path).parent
    if not directory.is_dir():
        return None
    matches = sorted(directory.glob("*_content_list.json"))
    return matches[0] if matches else None


def build_image_ref_map(md_content: str) -> dict[str, str]:
    """建立「图片文件名 → 增强后的 Markdown 图片引用」映射。

    增强后的 Markdown 形如 ``![视觉说明](http://…/upload-images/<stem>/<原名>.jpg)``，
    因此可从 URL 末段取回原始文件名，与 content_list 的 ``img_path`` 对应。
    """
    mapping: dict[str, str] = {}
    for alt, url in _IMAGE_REF_RE.findall(md_content or ""):
        clean_url = (url or "").split("?")[0].strip()
        if not clean_url:
            continue
        name = clean_url.rstrip("/").rsplit("/", 1)[-1]
        if name:
            mapping[name] = f"![{alt}]({url})"
    return mapping


def _clean_cell_text(cell) -> str:
    """单元格文本归一：折叠空白、转义竖线。"""
    text = cell.get_text(separator=" ", strip=True)
    text = re.sub(r"\s+", " ", text)
    return text.replace("|", "\\|")


def _expand_cells(raw_rows: list[list[tuple[str, int, int]]]) -> list[list[str]]:
    """把带 ``colspan`` / ``rowspan`` 的单元格展开为规整文本矩阵。

    ``rowspan`` 采用「向下重复文本」的近似补齐（复杂合并表的行错位属已知限制）。
    """
    grid: list[list[str]] = []
    pending: dict[int, list] = {}  # 列号 -> [文本, 剩余行数]
    for raw_row in raw_rows:
        row: list[str] = []
        column = 0
        iterator = iter(raw_row)
        while True:
            if column in pending:
                text, left = pending[column]
                row.append(text)
                if left <= 1:
                    pending.pop(column, None)
                else:
                    pending[column] = [text, left - 1]
                column += 1
                continue
            try:
                text, colspan, rowspan = next(iterator)
            except StopIteration:
                break
            for _ in range(max(colspan, 1)):
                row.append(text)
                if rowspan > 1:
                    pending[column] = [text, rowspan - 1]
                column += 1
        while column in pending:
            text, left = pending[column]
            row.append(text)
            if left <= 1:
                pending.pop(column, None)
            else:
                pending[column] = [text, left - 1]
            column += 1
        grid.append(row)
    return grid


def html_table_to_markdown(html: str) -> str:
    """把 MinerU 的 ``table_body``（HTML）转换为 Markdown 表格。

    无 ``<table>`` 或无法解析出数据行时返回空串，由调用方决定是否回退。
    """
    if not html or "<t" not in html.lower():
        return ""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    if table is None:
        return ""

    raw_rows: list[list[tuple[str, int, int]]] = []
    for tr in table.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        if not cells:
            continue
        raw_rows.append(
            [
                (
                    _clean_cell_text(cell),
                    int(cell.get("colspan") or 1),
                    int(cell.get("rowspan") or 1),
                )
                for cell in cells
            ]
        )
    if not raw_rows:
        return ""

    grid = _expand_cells(raw_rows)
    width = max(len(row) for row in grid)
    normalized = [row + [""] * (width - len(row)) for row in grid]

    def render(cells: list[str]) -> str:
        return "| " + " | ".join(cells) + " |"

    lines = [render(normalized[0]), "| " + " | ".join(["---"] * width) + " |"]
    lines.extend(render(row) for row in normalized[1:])
    return "\n".join(lines)


def _page_of(raw: dict) -> int | None:
    """取 1 起的页码；``page_idx`` 缺失时返回 None。"""
    page_index = raw.get("page_idx")
    if isinstance(page_index, int) and page_index >= 0:
        return page_index + 1
    return None


def _image_block_text(raw: dict, image_refs: dict[str, str]) -> str:
    """图片块的文本：优先用视觉说明引用，其次 caption，最后保留原始引用。"""
    img_path = (raw.get("img_path") or "").strip()
    name = img_path.rstrip("/").rsplit("/", 1)[-1]
    if name and name in image_refs:
        return image_refs[name]

    captions = [str(item).strip() for item in (raw.get("image_caption") or []) if str(item).strip()]
    if captions:
        return " ".join(captions)
    if img_path:
        return f"![]({img_path})"
    return ""


def normalize_content_list(blocks: list[dict] | None, image_refs: dict[str, str] | None = None) -> list[Block]:
    """把 MinerU content_list 归一化为 Block 流。"""
    refs = image_refs or {}
    normalized: list[Block] = []

    for raw in blocks or []:
        if not isinstance(raw, dict):
            continue
        block_type = str(raw.get("type") or "").strip()
        if block_type in _DROPPED_TYPES:
            continue
        page = _page_of(raw)

        if block_type == "table":
            markdown = html_table_to_markdown(raw.get("table_body") or "")
            if markdown:
                normalized.append(Block("table", markdown, None, page))
            continue

        if block_type == "image":
            text = _image_block_text(raw, refs)
            if text:
                normalized.append(Block("image", text, None, page))
            continue

        text = str(raw.get("text") or "").strip()
        if not text:
            continue
        level = raw.get("text_level")
        if isinstance(level, int) and level > 0:
            normalized.append(Block("heading", text, level, page))
        else:
            normalized.append(Block("text", text, None, page))

    return normalized
