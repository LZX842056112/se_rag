"""对外引用构造测试（读链路与 API 层共用同一实现）。"""
from __future__ import annotations

from app.rag.query.citations import build_citations, citations_from_reranked_docs, split_cited


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
