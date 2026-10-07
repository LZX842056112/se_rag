"""章节与子块切分（两级切片核心，纯函数）。

两级结构：

- **父块（章节级）**：一个标题对应一个父块，``content`` 为该标题的整棵子树文本（含嵌套
  子标题内容），仅用于检索命中后回填背景，不参与向量检索。无正文的章节标题同样产出父块，
  因此「章标题丢失」不再发生。
- **子块（检索单元）**：由**本章节自身**的原子块（段落 / 表格 / 代码 / 图片）贪心打包而成，
  **不含**嵌套子章节的文本——嵌套子章节会产出自己的子块，否则同一段文本会重复入库。

长度口径：所有阈值（``CHUNK_SIZE`` / ``CHUNK_MIN`` / ``CHUNK_MAX_SIZE``）一律按**正文口径**
比较，即不含前置标题的那部分文本。历史实现用「含标题前缀」的长度判断，导致短块合并永远
不可达（死代码），且正文仅超出阈值 1 个字符就产出碎片块。

重叠策略：仅在「单个原子块超长、必须从中间切开」时保留 ``CHUNK_OVERLAP`` 尾部重叠；原子块
之间的打包边界本身已落在段落 / 表格 / 代码边界上，不再额外重叠（命中后由父块提供上下文）。
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any

from langchain_text_splitters import RecursiveCharacterTextSplitter

from app.rag.import_.config import CHUNK_MAX_SIZE, CHUNK_MIN, CHUNK_OVERLAP, CHUNK_SIZE
from app.rag.import_.ids import make_chunk_id, make_content_hash, make_parent_id, section_key

# 正文切分分隔符。用正则模式匹配：
# - 中文句末/句内标点全量补齐（历史实现缺「，、：」，无句末标点的中文长句会退化为按字符硬切）
# - 点号要求后接空白，避免在 config.py / 3.5 / 10.1.1.1 处误切（历史缺陷 P1-2）
TEXT_SEPARATORS = ["\n\n", "\n", "。", "！", "？", "；", "，", "、", "：", "…", r"\.\s", r"[!?;,]\s", ""]

# 代码块切分只按行，避免把标识符/字符串切开
CODE_SEPARATORS = ["\n\n", "\n", ""]


@dataclass(frozen=True)
class Block:
    """归一化后的文档块。

    两条解析路径（MinerU content_list / Markdown 回退）产出同构的 Block 流，后续章节识别与
    子块切分完全共用。

    :param type: ``heading`` / ``text`` / ``table`` / ``code`` / ``image``
    :param text: 块文本。图片块为增强后的 Markdown 引用 ``![说明](公网地址)``，
                 既让说明可被检索，也保留地址供答案展示图片
    :param level: 标题层级（仅 ``heading`` 有值）
    :param page: 页码（1 起，未知为 None）
    """

    type: str
    text: str
    level: int | None = None
    page: int | None = None


def _text_splitter(*, separators: list[str], regex: bool, overlap: int = CHUNK_OVERLAP):
    """构造递归字符切割器。"""
    return RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE,
        chunk_overlap=overlap,
        separators=separators,
        is_separator_regex=regex,
    )


def _split_table(table_markdown: str) -> list[str]:
    """拆分超长 Markdown 表格，并给每个分块**复制表头与分隔行**。

    历史缺陷 P0-4：表格曾被当作普通行参与递归切割，后续分块丢失表头，成为无列语义的
    HTML/文本碎片。
    """
    lines = [line for line in table_markdown.split("\n") if line.strip()]
    if len(lines) < 3:
        # 结构不完整（无表头或只有表头），退化为普通文本切分
        return _text_splitter(separators=TEXT_SEPARATORS, regex=True).split_text(table_markdown)

    header, divider, rows = lines[0], lines[1], lines[2:]
    prefix_len = len(header) + len(divider) + 2  # 含两个换行
    parts: list[str] = []
    current: list[str] = []
    current_len = prefix_len

    for row in rows:
        added = len(row) + 1
        if current and current_len + added > CHUNK_SIZE:
            parts.append("\n".join([header, divider, *current]))
            current = []
            current_len = prefix_len
        current.append(row)
        current_len += added
    if current:
        parts.append("\n".join([header, divider, *current]))
    return parts or [table_markdown]


def _split_code(code_text: str) -> list[str]:
    """拆分超长代码块，并为每个分块补上围栏（避免出现未闭合的围栏）。"""
    lines = code_text.split("\n")
    fence = lines[0].strip() if lines and lines[0].strip().startswith(("```", "~~~")) else ""
    if not fence:
        return _text_splitter(separators=CODE_SEPARATORS, regex=False).split_text(code_text)

    body = "\n".join(lines[1:-1]) if len(lines) > 2 else ""
    parts = _text_splitter(separators=CODE_SEPARATORS, regex=False).split_text(body)
    return [f"{fence}\n{part}\n{fence}" for part in parts if part.strip()] or [code_text]


def _split_oversized(block: Block) -> list[str]:
    """拆分超长原子块（表格复制表头、代码补围栏、其余按标点切分）。"""
    if block.type == "table":
        return _split_table(block.text)
    if block.type == "code":
        return _split_code(block.text)
    return _text_splitter(separators=TEXT_SEPARATORS, regex=True).split_text(block.text)


def _pack_blocks(blocks: list[Block]) -> list[str]:
    """把章节自身的原子块贪心打包为若干正文段（长度不含标题前缀）。

    两种长度口径分工明确：

    - ``CHUNK_SIZE``（软目标）——决定**多个**原子块如何打包；
    - ``CHUNK_MAX_SIZE``（硬上限）——只有**单个**原子块超过硬上限才从中间切开。

    单个原子块落在 [CHUNK_SIZE, CHUNK_MAX_SIZE] 之间时**不切**：它本身已是一个语义完整的
    段落/表格/代码，按字符从中切开只会制造一个纯重叠的碎片尾巴（历史缺陷 P1-5：正文仅超出
    阈值 1 个字符就产出 [600, 56] 两块）。
    """
    parts: list[str] = []
    current: list[str] = []
    current_len = 0

    def flush() -> None:
        nonlocal current, current_len
        if current:
            parts.append("\n\n".join(current))
            current = []
            current_len = 0

    for block in blocks:
        text = (block.text or "").strip()
        if not text:
            continue
        if len(text) > CHUNK_MAX_SIZE:
            # 单个原子块超过硬上限：先结算已累积内容，再单独拆分该块
            flush()
            parts.extend(_split_oversized(block))
            continue
        added = len(text) + (2 if current else 0)
        if current and current_len + added > CHUNK_SIZE:
            flush()
            added = len(text)
        current.append(text)
        current_len += added
    flush()
    return parts


def _merge_short_parts(parts: list[str]) -> list[str]:
    """合并同章节内的过短正文段。

    合并条件：任一侧正文短于 ``CHUNK_MIN``，且合并后不超过 ``CHUNK_MAX_SIZE``。同时覆盖
    「前段偏短」与「尾段偏短」两种碎片形态（历史实现只处理前者，且因长度口径错误从未生效）。
    """
    merged: list[str] = []
    base: str | None = None
    for part in parts:
        if base is None:
            base = part
            continue
        either_short = len(base) < CHUNK_MIN or len(part) < CHUNK_MIN
        if either_short and len(base) + len(part) + 2 <= CHUNK_MAX_SIZE:
            base = f"{base}\n\n{part}"
            continue
        merged.append(base)
        base = part
    if base is not None:
        merged.append(base)
    return merged


def split_section_children(section: dict[str, Any], *, seq_start: int = 0) -> list[dict[str, Any]]:
    """把一个章节自身的原子块切成子块（检索单元）。

    :param section: ``build_sections`` 产出的章节字典
    :param seq_start: 本批子块的全局起始序号
    """
    title = section["title"]
    heading_path = section["heading_path"]
    occurrence = section["occurrence"]
    doc_key = section["doc_id"]
    file_title = section["file_title"]
    page = section.get("page")

    bodies = _merge_short_parts(_pack_blocks(section.get("blocks") or []))

    children: list[dict[str, Any]] = []
    for offset, body in enumerate(bodies):
        part = offset + 1
        content = f"{title}\n{body}"
        children.append(
            {
                "chunk_id": make_chunk_id(doc_key, heading_path, occurrence, part),
                "doc_id": doc_key,
                "parent_id": section["parent_id"],
                "file_title": file_title,
                "title": title,
                "parent_title": title,  # 兼容既有字段语义（最近标题文本）
                "heading_path": heading_path,
                "part": part,
                "seq": seq_start + offset,
                "page": page if page is not None else 0,
                "content": content,
                "content_hash": make_content_hash(content),
            }
        )
    return children


def _new_section(*, level: int, title: str, heading_path: str, occurrence: int, page: int | None) -> dict:
    """构造一个章节节点（尚未挂 parent_id 等文档级字段）。"""
    return {
        "level": level,
        "title": title,
        "heading_path": heading_path,
        "occurrence": occurrence,
        "page": page,
        "blocks": [],
    }


def split_blocks(blocks: list[Block], doc_key: str, file_title: str) -> tuple[list[dict], list[dict]]:
    """两级切分的纯函数编排：``(parents, children)``，并回填父块的 ``child_count``。

    导入链路（``split_service.split_document``）与评估链路（``rag_eval.split_dataset``）共用
    本函数，保证「评估用的切片」与「线上入库的切片」出自同一套逻辑。
    """
    parents, sections = build_sections(blocks, doc_key, file_title)
    children: list[dict[str, Any]] = []
    for section in sections:
        children.extend(split_section_children(section, seq_start=len(children)))

    counts = Counter(child["parent_id"] for child in children)
    for parent in parents:
        parent["child_count"] = counts.get(parent["parent_id"], 0)
    return parents, children


def build_sections(blocks: list[Block], doc_key: str, file_title: str) -> tuple[list[dict], list[dict]]:
    """把 Block 流切分为章节，并产出「父块」与「章节（子块输入）」。

    章节边界：本标题起，到**下一个层级 ≤ 本标题层级**的标题之前。因此：

    - 无正文的章节标题（如 ``# 第3章`` 紧跟 ``## 3.1``）仍产出父块，标题不丢（修 P0-2）；
    - 父块 ``content`` 为整棵子树文本（提供完整背景），子块只取章节自身文本（避免重复入库）。

    :return: ``(parents, sections)``；``sections`` 可直接交给 ``split_section_children``
    """
    occurrence_counter: dict[str, int] = {}

    def next_occurrence(path: str) -> int:
        count = occurrence_counter.get(path, 0)
        occurrence_counter[path] = count + 1
        return count

    first_page = next((b.page for b in blocks if b.page is not None), None)
    # 前言章节：标题取文件名，层级 0；与任何标题共用同一套 occurrence 计数，避免 section_key 撞车
    sections: list[dict] = [
        _new_section(
            level=0,
            title=file_title,
            heading_path=file_title,
            occurrence=next_occurrence(file_title),
            page=first_page,
        )
    ]
    stack: list[int] = [0]

    for block in blocks:
        if block.type == "heading":
            level = block.level or 1
            while len(stack) > 1 and sections[stack[-1]]["level"] >= level:
                stack.pop()
            ancestors = [sections[index]["title"] for index in stack[1:]]
            heading_path = " / ".join([*ancestors, block.text])
            sections.append(
                _new_section(
                    level=level,
                    title=block.text,
                    heading_path=heading_path,
                    occurrence=next_occurrence(heading_path),
                    page=block.page,
                )
            )
            stack.append(len(sections) - 1)
        else:
            sections[stack[-1]]["blocks"].append(block)

    def subtree_blocks(index: int) -> list[Block]:
        """本节点自身块 + 所有后代节点自身块（前言只取自身）。"""
        node = sections[index]
        collected = list(node["blocks"])
        if node["level"] <= 0:
            return collected
        cursor = index + 1
        while cursor < len(sections) and sections[cursor]["level"] > node["level"]:
            collected.extend(sections[cursor]["blocks"])
            cursor += 1
        return collected

    parents: list[dict] = []
    for index, node in enumerate(sections):
        parent_id = make_parent_id(doc_key, node["heading_path"], node["occurrence"])
        node["parent_id"] = parent_id
        node["doc_id"] = doc_key
        node["file_title"] = file_title
        node["section_key"] = section_key(node["heading_path"], node["occurrence"])
        body = "\n\n".join(b.text for b in subtree_blocks(index) if (b.text or "").strip())
        parents.append(
            {
                "parent_id": parent_id,
                "doc_id": doc_key,
                "file_title": file_title,
                "title": node["title"],
                "heading_path": node["heading_path"],
                "page": node["page"] if node["page"] is not None else 0,
                "child_count": 0,  # 由调用方按实际子块数回填
                "content": f"{node['title']}\n{body}" if body else node["title"],
            }
        )
    return parents, sections
