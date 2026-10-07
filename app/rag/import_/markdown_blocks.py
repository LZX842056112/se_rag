"""Markdown → 归一化 Block 流（回退路径）。

当 MinerU 的结构化产物 ``content_list.json`` 缺失时（存量文档未重跑解析），仍需要一套与结构
感知路径**同构**的 Block 流，以便章节识别与子块切分完全共用。本模块修掉历史实现的四类失真：

1. **行内容不再 strip**——此前所有入块行都用 ``line.strip()``，代码块缩进与嵌套列表层级被抹平；
2. **空行不再丢弃**——此前空行被 ``continue`` 跳过，段落边界信息消失；
3. **代码块整段保真**——围栏状态机把 ``` / ~~~ 之间的内容逐字保留（含空行与缩进）；
4. **表格原子化**——同时识别 MinerU 直接输出的 ``<table>…</table>`` HTML 表与手写 Markdown 的
   管道表；此前表格被当作普通行参与字符切割，长表被切碎且后续分块丢失表头。

已知限制：不支持 Setext 风格标题（下划线式），代码块内部出现围栏会提前闭合。
"""
from __future__ import annotations

import re

from app.rag.import_.content_list import html_table_to_markdown
from app.rag.import_.section_splitter import Block

# ATX 标题：# ~ ###### 后跟至少一个空白
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
# 整行仅为一个 Markdown 图片引用
_IMAGE_LINE_RE = re.compile(r"^!\[.*?\]\(.*?\)\s*$")
# Markdown 表格的分隔行（第二行），用于确认连续的管道行确实构成表格
_TABLE_DIVIDER_RE = re.compile(r"^\|[\s:|-]+\|$")


def normalize_markdown(md_content: str) -> list[Block]:
    """把 Markdown 文本归一化为 Block 流。"""
    text = (md_content or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")

    blocks: list[Block] = []
    paragraph: list[str] = []
    code_lines: list[str] = []
    table_lines: list[str] = []
    pipe_lines: list[str] = []
    in_fence = False
    fence_marker = ""
    in_table = False

    def flush_paragraph() -> None:
        if paragraph:
            joined = "\n".join(paragraph).strip("\n")
            if joined.strip():
                blocks.append(Block("text", joined))
            paragraph.clear()

    def flush_table() -> None:
        html = "\n".join(table_lines)
        table_lines.clear()
        markdown = html_table_to_markdown(html)
        blocks.append(Block("table", markdown or html))

    def flush_pipe() -> None:
        """结算连续管道行：第二行为分隔行时视为 Markdown 表格，否则退回普通文本。"""
        if not pipe_lines:
            return
        if len(pipe_lines) >= 2 and _TABLE_DIVIDER_RE.match(pipe_lines[1].strip()):
            blocks.append(Block("table", "\n".join(pipe_lines)))
        else:
            blocks.append(Block("text", "\n".join(pipe_lines)))
        pipe_lines.clear()

    for line in lines:
        stripped = line.strip()
        is_fence = stripped.startswith("```") or stripped.startswith("~~~")

        # 1. 代码块内部：逐字保留
        if in_fence:
            code_lines.append(line)
            if is_fence and stripped.startswith(fence_marker):
                in_fence = False
                blocks.append(Block("code", "\n".join(code_lines)))
                code_lines = []
            continue

        # 2. 代码块开始
        if is_fence:
            flush_paragraph()
            flush_pipe()
            in_fence = True
            fence_marker = stripped[:3]
            code_lines = [line]
            continue

        # 3. HTML 表格（可能跨行）
        if in_table:
            table_lines.append(line)
            if "</table>" in line.lower():
                in_table = False
                flush_table()
            continue
        if "<table" in line.lower():
            flush_paragraph()
            flush_pipe()
            in_table = True
            table_lines.append(line)
            if "</table>" in line.lower():
                in_table = False
                flush_table()
            continue

        # 4. Markdown 管道表格（连续以 | 开头的行）
        if stripped.startswith("|"):
            flush_paragraph()
            pipe_lines.append(line)
            continue
        flush_pipe()

        # 5. 空行：段落分隔符，保留语义但不产出块
        if not stripped:
            flush_paragraph()
            continue

        # 6. 标题
        heading = _HEADING_RE.match(stripped)
        if heading:
            flush_paragraph()
            blocks.append(Block("heading", heading.group(2).strip(), len(heading.group(1))))
            continue

        # 7. 独占一行的图片（增强后 alt 即视觉模型生成的说明，url 为公网地址）
        if _IMAGE_LINE_RE.match(stripped):
            flush_paragraph()
            blocks.append(Block("image", stripped))
            continue

        paragraph.append(line)

    # 收尾
    if in_fence and code_lines:
        blocks.append(Block("code", "\n".join(code_lines)))
    if in_table and table_lines:
        flush_table()
    flush_pipe()
    flush_paragraph()
    return blocks
