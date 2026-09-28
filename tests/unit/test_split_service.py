"""文档切分测试：标题粗切、代码块保护、超长细切与短块合并。"""
from __future__ import annotations

from app.rag.import_.split_service import refine_chunks, split_chunks_document


def test_split_by_headings():
    md = "# 标题一\n内容一\n\n# 标题二\n内容二\n"
    chunks = split_chunks_document(md, "doc")
    assert [c["title"] for c in chunks] == ["# 标题一", "# 标题二"]
    assert chunks[0]["file_title"] == "doc"
    assert "内容一" in chunks[0]["content"]


def test_code_block_hash_is_not_a_heading():
    md = "# 标题\n```python\n# 这是注释不是标题\nprint(1)\n```\n正文\n"
    chunks = split_chunks_document(md, "doc")
    assert len(chunks) == 1
    assert "print(1)" in chunks[0]["content"]


def test_document_without_heading_falls_back_to_default_title():
    chunks = split_chunks_document("只有正文，没有任何标题", "doc")
    assert len(chunks) == 1
    assert chunks[0]["title"] == "default"


def test_long_chunk_is_split_with_parent_title_and_part():
    long_body = "# 长标题\n" + ("内容" * 400)
    refined = refine_chunks([{"title": "# 长标题", "content": long_body, "file_title": "doc"}])
    assert len(refined) > 1
    assert all(c["parent_title"] == "# 长标题" for c in refined)
    assert [c["part"] for c in refined] == list(range(1, len(refined) + 1))


def test_short_chunks_with_same_parent_are_merged():
    chunks = [
        {"title": "A_第1部分", "parent_title": "A", "part": 1, "content": "A\n短内容", "file_title": "doc"},
        {"title": "A_第2部分", "parent_title": "A", "part": 2, "content": "A\n另一段", "file_title": "doc"},
    ]
    refined = refine_chunks(chunks)
    assert len(refined) == 1
    assert "短内容" in refined[0]["content"] and "另一段" in refined[0]["content"]


def test_short_chunks_with_different_parent_are_not_merged():
    chunks = [
        {"title": "A", "parent_title": "A", "part": 1, "content": "A\n短内容"},
        {"title": "B", "parent_title": "B", "part": 1, "content": "B\n另一段"},
    ]
    assert len(refine_chunks(chunks)) == 2
