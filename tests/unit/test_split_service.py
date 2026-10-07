"""文档切分服务测试：入口编排、解析路径选择、父子块产出与备份。

历史的 ``split_chunks_document`` / ``refine_chunks`` 三段式 API 已被两级切片替代，
原用例中「直接构造带 parent_title 的输入再断言合并」属于生产链路无法到达的路径
（假信心用例），已改写为经真实入口的断言。
"""
from __future__ import annotations

import json

import pytest

from app.process.import_.agent.state import create_default_state
from app.rag.import_.split_service import resolve_blocks, split_document


def run_split(tmp_path, md: str, file_title: str = "doc"):
    """把 Markdown 落盘后经真实入口切分，返回 state。"""
    md_path = tmp_path / f"{file_title}.md"
    md_path.write_text(md, encoding="utf-8")
    state = create_default_state(file_title=file_title, md_path=str(md_path), md_content=md)
    return split_document(state)


def test_split_by_headings(tmp_path):
    state = run_split(tmp_path, "# 标题一\n\n内容一\n\n# 标题二\n\n内容二\n")
    titles = [c["title"] for c in state["chunks"]]
    assert "标题一" in titles and "标题二" in titles
    assert state["chunks"][0]["file_title"] == "doc"


def test_code_block_hash_is_not_a_heading(tmp_path):
    md = "# 标题\n\n```python\n# 这是注释不是标题\nprint(1)\n```\n\n正文\n"
    state = run_split(tmp_path, md)
    assert [c["title"] for c in state["chunks"]] == ["标题"]
    assert "print(1)" in state["chunks"][0]["content"]


def test_document_without_heading_uses_file_title(tmp_path):
    state = run_split(tmp_path, "只有正文，没有任何标题", file_title="说明书")
    assert len(state["chunks"]) == 1
    assert state["chunks"][0]["title"] == "说明书"
    assert state["parent_chunks"][0]["title"] == "说明书"


def test_long_section_is_split_with_shared_parent_id(tmp_path):
    md = "# 长标题\n\n" + "。".join(["内容片段" * 30 for _ in range(20)]) + "\n"
    state = run_split(tmp_path, md)
    chunks = state["chunks"]
    assert len(chunks) > 1
    assert len({c["parent_id"] for c in chunks}) == 1
    assert [c["part"] for c in chunks] == list(range(1, len(chunks) + 1))
    assert all(c["title"] == "长标题" for c in chunks)


def test_chunks_from_different_sections_are_never_merged(tmp_path):
    md = "# A\n\n甲内容。\n\n# B\n\n乙内容。\n"
    state = run_split(tmp_path, md)
    assert len(state["chunks"]) == 2
    assert len({c["parent_id"] for c in state["chunks"]}) == 2


def test_parent_child_counts_are_consistent(tmp_path):
    md = "# A\n\n" + ("甲" * 700) + "\n\n# B\n\n乙内容。\n"
    state = run_split(tmp_path, md)
    counted = {}
    for child in state["chunks"]:
        counted[child["parent_id"]] = counted.get(child["parent_id"], 0) + 1
    for parent in state["parent_chunks"]:
        assert parent["child_count"] == counted.get(parent["parent_id"], 0)


def test_backups_are_written_next_to_markdown(tmp_path):
    md = "# 标题\n\n内容。\n"
    state = run_split(tmp_path, md, file_title="手册")
    assert json.loads((tmp_path / "手册.json").read_text(encoding="utf-8"))
    parents_backup = json.loads((tmp_path / "手册_parents.json").read_text(encoding="utf-8"))
    assert parents_backup[0]["parent_id"] == state["parent_chunks"][0]["parent_id"]


def test_empty_output_fails_loudly(tmp_path):
    """切分无产出时必须显式失败，不得静默回退到陈旧备份。"""
    md_path = tmp_path / "空.md"
    md_path.write_text("   \n\n  \n", encoding="utf-8")
    state = create_default_state(file_title="空", md_path=str(md_path), md_content="   \n\n  \n")
    with pytest.raises(ValueError):
        split_document(state)


def test_resolve_blocks_prefers_content_list(tmp_path):
    """存在 MinerU 结构化产物时优先使用，可丢弃页眉页脚并保留表格结构。"""
    md_path = tmp_path / "doc.md"
    md_path.write_text("# 标题\n\n正文。\n", encoding="utf-8")
    raw = [
        {"type": "header", "text": "brother", "page_idx": 0},
        {"type": "text", "text": "标题", "text_level": 2, "page_idx": 0},
        {"type": "text", "text": "正文。", "page_idx": 0},
        {"type": "footer", "text": "© 版权", "page_idx": 0},
    ]
    (tmp_path / "abc_content_list.json").write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    blocks, source = resolve_blocks(md_path.read_text(encoding="utf-8"), str(md_path))
    assert source == "content_list"
    assert [b.type for b in blocks] == ["heading", "text"]


def test_resolve_blocks_falls_back_to_markdown(tmp_path):
    md_path = tmp_path / "doc.md"
    md_path.write_text("# 标题\n\n正文。\n", encoding="utf-8")
    blocks, source = resolve_blocks(md_path.read_text(encoding="utf-8"), str(md_path))
    assert source == "markdown"
    assert [b.type for b in blocks] == ["heading", "text"]


def test_resolve_blocks_falls_back_when_content_list_is_broken(tmp_path):
    """产物损坏时回退 Markdown，不中断导入。"""
    md_path = tmp_path / "doc.md"
    md_path.write_text("# 标题\n\n正文。\n", encoding="utf-8")
    (tmp_path / "abc_content_list.json").write_text("{ 不是合法 json", encoding="utf-8")

    blocks, source = resolve_blocks(md_path.read_text(encoding="utf-8"), str(md_path))
    assert source == "markdown"
    assert blocks
