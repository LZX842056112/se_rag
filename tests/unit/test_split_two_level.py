"""两级切片结构测试：章节识别、父块、重名标题消歧、父子关联。"""
from __future__ import annotations

from app.rag.import_.ids import make_doc_id
from app.rag.import_.markdown_blocks import normalize_markdown
from app.rag.import_.section_splitter import build_sections, split_section_children


def run_split(md: str, file_title: str = "doc"):
    """跑完整两级切分，返回 (parents, children)。"""
    doc_key = make_doc_id(file_title)
    parents, sections = build_sections(normalize_markdown(md), doc_key, file_title)
    children = []
    for section in sections:
        children.extend(split_section_children(section, seq_start=len(children)))
    return parents, children


def body_of(chunk: dict) -> str:
    """去掉标题前缀后的正文。"""
    return chunk["content"][len(chunk["title"]) + 1:]


def test_duplicate_headings_produce_distinct_parent_blocks():
    """手册中「设备」「电源线」等标题在不同父章节下重复出现，必须各自独立。"""
    md = (
        "# 第一部分\n\n## 设备\n\n第一部分设备说明。\n\n"
        "# 第二部分\n\n## 设备\n\n第二部分设备说明。\n\n"
        "# 第三部分\n\n## 设备\n\n第三部分设备说明。\n"
    )
    parents, _ = run_split(md)
    devices = [p for p in parents if p["title"] == "设备"]
    assert len(devices) == 3
    assert len({p["parent_id"] for p in devices}) == 3
    assert len({p["heading_path"] for p in devices}) == 3


def test_every_child_has_a_resolvable_parent():
    md = "# 章节A\n\n内容A。\n\n## 子章节A1\n\n内容A1。\n\n# 章节B\n\n内容B。\n"
    parents, children = run_split(md)
    parent_ids = {p["parent_id"] for p in parents}
    assert children
    assert all(c["parent_id"] in parent_ids for c in children)


def test_children_do_not_duplicate_nested_section_text():
    """子块只取章节自身文本；嵌套子章节的文本由其自身子块承载，否则会重复入库。"""
    md = "# 父章节\n\n父章节自身的正文段落。\n\n## 子章节\n\n子章节的正文内容。\n"
    _, children = run_split(md)
    joined = "\n".join(body_of(c) for c in children)
    assert joined.count("子章节的正文内容。") == 1
    assert joined.count("父章节自身的正文段落。") == 1


def test_parent_content_covers_whole_subtree():
    """父块用于回填背景，必须包含整棵子树的文本。"""
    md = "# 父章节\n\n父章节自身的正文段落。\n\n## 子章节\n\n子章节的正文内容。\n"
    parents, _ = run_split(md)
    parent = next(p for p in parents if p["title"] == "父章节")
    assert "父章节自身的正文段落。" in parent["content"]
    assert "子章节的正文内容。" in parent["content"]


def test_heading_path_is_a_breadcrumb():
    md = "# 第3章 网络配置\n\n## 3.1 接口配置\n\n### 3.1.1 二层模式\n\n以太网接口默认工作在二层模式。\n"
    _, children = run_split(md)
    deepest = next(c for c in children if c["title"] == "3.1.1 二层模式")
    assert deepest["heading_path"] == "第3章 网络配置 / 3.1 接口配置 / 3.1.1 二层模式"


def test_preamble_gets_its_own_parent_and_children():
    md = "封面与版权声明段落。\n\n# 第一章\n\n正文内容。\n"
    parents, children = run_split(md, file_title="手册")
    assert parents[0]["title"] == "手册"
    preamble_children = [c for c in children if c["parent_id"] == parents[0]["parent_id"]]
    assert preamble_children
    assert "封面与版权声明段落。" in body_of(preamble_children[0])


def test_seq_is_globally_continuous_and_part_is_per_section():
    md = "# A\n\n" + ("甲" * 500) + "\n\n" + ("乙" * 500) + "\n\n# B\n\n乙章节内容。\n"
    _, children = run_split(md)
    assert [c["seq"] for c in children] == list(range(len(children)))
    part_a = [c["part"] for c in children if c["title"] == "A"]
    assert part_a == list(range(1, len(part_a) + 1))
    assert all(c["part"] == 1 for c in children if c["title"] == "B")


def test_chunk_ids_are_unique_within_document():
    md = "# A\n\n内容甲。\n\n# B\n\n内容乙。\n\n# C\n\n内容丙。\n"
    _, children = run_split(md)
    ids = [c["chunk_id"] for c in children]
    assert len(set(ids)) == len(ids)
