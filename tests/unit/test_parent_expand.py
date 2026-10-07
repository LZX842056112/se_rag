"""父块回溯测试：来源过滤、去重、预算截断、字段透传。"""
from __future__ import annotations

from app.rag.query import parent_expand_service, rerank_service
from app.rag.query.config import PARENT_MAX_CHARS, PARENT_TOTAL_BUDGET_CHARS


def kb_doc(parent_id: str = "p1", score: float = 0.9, **overrides) -> dict:
    doc = {
        "chunk_id": f"c-{parent_id}",
        "text": "子块正文",
        "title": "子标题",
        "score": score,
        "type": "milvus",
        "source": "milvus",
        "parent_id": parent_id,
        "page": 3,
        "heading_path": "章节 / 子标题",
    }
    doc.update(overrides)
    return doc


def test_is_local_chunk_filters_non_kb_sources():
    assert parent_expand_service.is_local_chunk(kb_doc())
    assert not parent_expand_service.is_local_chunk(kb_doc(type="web", source="web"))
    assert not parent_expand_service.is_local_chunk(kb_doc(source="evolution"))
    assert not parent_expand_service.is_local_chunk(kb_doc(parent_id=None))
    assert not parent_expand_service.is_local_chunk(kb_doc(parent_id=""))


def test_expand_attaches_parent_content(monkeypatch):
    monkeypatch.setattr(
        parent_expand_service, "fetch_parents",
        lambda ids: {"p1": {"parent_id": "p1", "title": "章节", "content": "章节完整背景"}},
    )
    docs = parent_expand_service.expand_docs_with_parents([kb_doc()])
    assert docs[0]["parent_content"] == "章节完整背景"
    assert docs[0]["parent_title"] == "章节"


def test_expand_injects_each_parent_only_once(monkeypatch):
    monkeypatch.setattr(
        parent_expand_service, "fetch_parents",
        lambda ids: {"p1": {"parent_id": "p1", "title": "章节", "content": "背景"}},
    )
    docs = [kb_doc(score=0.9), kb_doc(score=0.8)]
    parent_expand_service.expand_docs_with_parents(docs)
    assert sum(1 for d in docs if d.get("parent_content")) == 1


def test_expand_skips_web_and_evolution_docs(monkeypatch):
    monkeypatch.setattr(
        parent_expand_service, "fetch_parents",
        lambda ids: {"p1": {"parent_id": "p1", "title": "章节", "content": "背景"}},
    )
    web = {"type": "web", "source": "web", "text": "联网片段", "chunk_id": None}
    evolution = {"type": "milvus", "source": "evolution", "parent_id": "", "text": "FAQ"}
    docs = [web, evolution, kb_doc()]
    parent_expand_service.expand_docs_with_parents(docs)
    assert "parent_content" not in web
    assert "parent_content" not in evolution
    assert docs[2]["parent_content"] == "背景"


def test_expand_truncates_oversized_parent(monkeypatch):
    long_content = "背" * (PARENT_MAX_CHARS + 500)
    monkeypatch.setattr(
        parent_expand_service, "fetch_parents",
        lambda ids: {"p1": {"parent_id": "p1", "title": "章节", "content": long_content}},
    )
    docs = parent_expand_service.expand_docs_with_parents([kb_doc()])
    assert len(docs[0]["parent_content"]) == PARENT_MAX_CHARS + 1  # 截断 + 省略号
    assert docs[0]["parent_content"].endswith("…")


def test_expand_respects_total_budget(monkeypatch):
    """预算按分数降序分配，耗尽后其余命中不再注入背景。"""
    chunk = PARENT_MAX_CHARS
    parents = {
        f"p{i}": {"parent_id": f"p{i}", "title": f"章节{i}", "content": "背" * chunk}
        for i in range(1, 6)
    }
    monkeypatch.setattr(parent_expand_service, "fetch_parents", lambda ids: parents)
    docs = [kb_doc(parent_id=f"p{i}", score=0.9 - i * 0.01) for i in range(1, 6)]
    parent_expand_service.expand_docs_with_parents(docs)

    injected = [d for d in docs if d.get("parent_content")]
    # 前 3 个用满单块上限（3×1200=3600），第 4 个只能用剩余额度并被截断，第 5 个被挤出
    assert len(injected) == 4
    assert injected[0]["parent_id"] == "p1"  # 分数最高者优先
    assert [len(d["parent_content"]) for d in injected[:3]] == [chunk] * 3
    assert sum(len(d["parent_content"]) for d in injected) <= PARENT_TOTAL_BUDGET_CHARS + len(injected)
    assert "parent_content" not in docs[4]


def test_expand_returns_docs_unchanged_when_no_parent_found(monkeypatch):
    monkeypatch.setattr(parent_expand_service, "fetch_parents", lambda ids: {})
    docs = [kb_doc()]
    assert parent_expand_service.expand_docs_with_parents(docs) == docs
    assert "parent_content" not in docs[0]


def test_expand_is_noop_without_local_chunks():
    web = {"type": "web", "source": "web", "text": "联网"}
    assert parent_expand_service.expand_docs_with_parents([web]) == [web]


def test_rerank_stage_passes_parent_metadata_through():
    """重排会重建 dict；parent_id / page 等必须透传，否则父块回溯无分组键可用。"""
    rrf_chunks = [
        {
            "chunk_id": "c1",
            "content": "子块正文",
            "title": "子标题",
            "source": "milvus",
            "parent_id": "p1",
            "page": 5,
            "heading_path": "章节 / 子标题",
            "file_title": "手册",
        }
    ]
    docs = rerank_service.deal_rrf_and_web_result(rrf_chunks, [])
    assert docs[0]["parent_id"] == "p1"
    assert docs[0]["page"] == 5
    assert docs[0]["heading_path"] == "章节 / 子标题"
    assert docs[0]["file_title"] == "手册"
    assert docs[0]["text"] == "子块正文"
