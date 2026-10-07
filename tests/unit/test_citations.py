"""对外引用构造测试（读链路与 API 层共用同一实现）。"""
from __future__ import annotations

from app.rag.query.citations import (
    build_citations,
    build_doc_meta,
    citations_from_reranked_docs,
    split_cited,
)


def test_build_citations_marks_source_and_dedupes():
    citations = build_citations(["11", "12", "11", "13"], ["12"])
    assert [c["faq_id"] for c in citations] == ["11", "12", "13"]
    assert [c["source"] for c in citations] == ["kb", "evolution", "kb"]


def test_build_citations_handles_empty_input():
    assert build_citations(None, None) == []


def test_web_citations_carry_source_title_and_url():
    """回归：答案靠联网片段答出来时，界面必须能标「联网」并给出原网页。"""
    citations = build_citations(
        ["11"], ["11"],
        [{"url": "https://example.com/a", "title": "官方规格页", "score": 0.88}],
    )
    assert [c["source"] for c in citations] == ["evolution", "web"]
    web = citations[1]
    assert web["faq_id"] == "https://example.com/a"
    assert web["title"] == "官方规格页"
    assert web["score"] == 0.88


def test_web_citations_skip_unsafe_or_missing_urls():
    """只接受 http/https：javascript:/data:/空 URL 一律不进引用。"""
    citations = build_citations([], [], [
        {"url": "javascript:alert(1)", "title": "坏链接"},
        {"url": "data:text/html;base64,xx", "title": "内联"},
        {"url": "", "title": "无链接"},
        {"url": "https://example.com/b", "title": ""},
    ])
    assert [c["faq_id"] for c in citations] == ["https://example.com/b"]
    assert citations[0]["source"] == "web"


def test_web_citations_dedupe_by_url():
    docs = [{"url": "https://example.com/a", "title": "一"}, {"url": "https://example.com/a", "title": "一"}]
    assert len(build_citations([], [], docs)) == 1


def test_split_cited_separates_web_from_kb():
    docs = [
        {"chunk_id": 1, "source": "milvus"},
        {"chunk_id": "evo_1", "source": "evolution"},
        {"chunk_id": None, "type": "web", "url": "https://example.com/a"},
        {"chunk_id": None, "source": "milvus"},  # 无主键的切片不进引用
    ]
    cited, evolution_ids, web_docs = split_cited(docs)
    assert cited == [1, "evo_1"]
    assert evolution_ids == ["evo_1"]
    assert [d["url"] for d in web_docs] == ["https://example.com/a"]


def test_citations_from_reranked_docs_covers_three_sources():
    docs = [
        {"chunk_id": 11, "source": "milvus"},
        {"chunk_id": 12, "source": "evolution"},
        {"chunk_id": None, "type": "web", "url": "https://example.com/a", "title": "网"},
    ]
    citations = citations_from_reranked_docs(docs)
    assert [c["source"] for c in citations] == ["kb", "evolution", "web"]


def test_citations_carry_page_and_heading_from_structured_metadata():
    """结构化切分带来的页码与章节面包屑必须出现在引用里，否则无法溯源到原文位置。"""
    docs = [
        {"chunk_id": "c1", "source": "milvus", "page": 12, "heading_path": "第3章 / 3.1 接口配置"},
        {"chunk_id": "c2", "source": "milvus", "page": 0, "heading_path": ""},
    ]
    citations = citations_from_reranked_docs(docs)
    assert citations[0]["page"] == 12
    assert citations[0]["heading"] == "第3章 / 3.1 接口配置"
    # page=0 表示未知页码（Markdown 回退路径），不应伪装成第 0 页
    assert citations[1]["page"] is None


def test_citations_fall_back_to_title_when_no_heading_path():
    citations = build_citations(
        ["c1"], doc_meta={"c1": {"title": "警告标签", "page": 2}},
    )
    assert citations[0]["heading"] == "警告标签"
    assert citations[0]["page"] == 2


def test_build_doc_meta_indexes_by_string_chunk_id():
    """Milvus 数值主键会被解析成 int，引用侧一律按 str 查表，避免查不到元数据。"""
    meta = build_doc_meta([{"chunk_id": 11, "page": 3}, {"chunk_id": None, "type": "web"}])
    assert set(meta) == {"11"}
    assert meta["11"]["page"] == 3
