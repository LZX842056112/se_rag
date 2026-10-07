"""切分质量指标（纯函数）。

补上历史评估链路的空白：原评估样本是**手工编写**的切片，切分器完全不在链路内，因此切分策略
怎么改都不会被评估发现。这里把「切分得好不好」变成可回归的数字。

碎片率口径：结构感知切分下，短小节会天然产出短切片——不同标题的切片不合并，以保证切片标题与
引用溯源精确（合并会让标题与实际内容不符）。因此验收看的是碎片块**承载的内容占比**，而不是
碎片块的数量占比。
"""
from __future__ import annotations

from typing import Any

from app.rag.import_.config import CHUNK_MIN


def body_of(chunk: dict[str, Any]) -> str:
    """去掉切片前置标题后的正文。"""
    title = chunk.get("title") or ""
    content = chunk.get("content") or ""
    prefix = f"{title}\n"
    return content[len(prefix):] if content.startswith(prefix) else content


def line_coverage(blocks: list, children: list[dict]) -> float:
    """源内容覆盖率：源块文本是否都能在某个切片中找到（目标 100%）。

    标题块额外允许命中切片的 ``heading_path``——无正文的章节标题只作为父块存在，其文本会出现在
    后代切片的标题面包屑里。
    """
    haystack = "\n".join(child.get("content") or "" for child in children)
    paths = "\n".join(child.get("heading_path") or "" for child in children)
    sources = [block.text for block in blocks if (block.text or "").strip() and block.type != "table"]
    if not sources:
        return 1.0
    covered = 0
    for block in blocks:
        text = (block.text or "").strip()
        if not text or block.type == "table":
            continue
        if text in haystack or (block.type == "heading" and text in paths):
            covered += 1
    return round(covered / len(sources), 4)


def table_integrity(blocks: list, children: list[dict]) -> float:
    """表格完整率：每张表的数据行都必须出现在「带表头」的切片里。

    历史缺陷是长表被字符切割后，后续分块只剩无表头的行碎片——对检索与作答都是噪声。
    """
    tables = [block.text for block in blocks if block.type == "table" and (block.text or "").strip()]
    if not tables:
        return 1.0
    contents = [child.get("content") or "" for child in children]
    intact = 0
    for table in tables:
        lines = [line for line in table.split("\n") if line.strip()]
        if len(lines) < 3:
            intact += 1
            continue
        header, data_rows = lines[0], lines[2:]
        ok = all(any(row in content and header in content for content in contents) for row in data_rows)
        intact += 1 if ok else 0
    return round(intact / len(tables), 4)


def fragment_rate(children: list[dict], *, min_chars: int = CHUNK_MIN) -> dict[str, float]:
    """碎片率：分别给出「碎片块数量占比」与「碎片块承载的内容占比」。"""
    bodies = [body_of(child) for child in children]
    total_chars = sum(len(body) for body in bodies)
    fragment_chars = sum(len(body) for body in bodies if len(body) < min_chars)
    return {
        "min_chars": min_chars,
        "chunk_ratio": round(sum(1 for body in bodies if len(body) < min_chars) / len(bodies), 4) if bodies else 0.0,
        "content_ratio": round(fragment_chars / total_chars, 4) if total_chars else 0.0,
    }


def heading_path_coverage(children: list[dict]) -> float:
    """带章节面包屑的切片占比（结构感知路径应接近 1，纯 Markdown 回退路径同样有面包屑）。"""
    if not children:
        return 0.0
    return round(sum(1 for child in children if (child.get("heading_path") or "").strip()) / len(children), 4)


def page_provenance_rate(children: list[dict]) -> float:
    """带页码的切片占比；Markdown 回退路径拿不到页码，故该值可能为 0。"""
    if not children:
        return 0.0
    return round(sum(1 for child in children if int(child.get("page") or 0) > 0) / len(children), 4)


def parent_child_integrity(parents: list[dict], children: list[dict]) -> dict[str, float]:
    """父子关联完整性：每个子块的 parent_id 都能在父块列表中找到。"""
    parent_ids = {parent.get("parent_id") for parent in parents}
    resolvable = sum(1 for child in children if child.get("parent_id") in parent_ids)
    return {
        "resolvable_ratio": round(resolvable / len(children), 4) if children else 0.0,
        "parent_count": len(parents),
        "child_count": len(children),
    }


def evaluate_split_quality(
    children: list[dict],
    parents: list[dict],
    blocks: list | None = None,
) -> dict[str, Any]:
    """汇总切分质量指标。"""
    bodies = [len(body_of(child)) for child in children]
    quality: dict[str, Any] = {
        "子块数": len(children),
        "父块数": len(parents),
        "正文字长": {
            "最小": min(bodies) if bodies else 0,
            "中位数": sorted(bodies)[len(bodies) // 2] if bodies else 0,
            "最大": max(bodies) if bodies else 0,
        },
        "碎片率": fragment_rate(children),
        "章节面包屑覆盖率": heading_path_coverage(children),
        "页码溯源覆盖率": page_provenance_rate(children),
        "父子关联": parent_child_integrity(parents, children),
    }
    if blocks is not None:
        quality["源内容覆盖率"] = line_coverage(blocks, children)
        quality["表格完整率"] = table_integrity(blocks, children)
    return quality
