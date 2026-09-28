"""重排动态截断测试（累计断崖口径）。"""
from __future__ import annotations

from app.rag.query.rerank_service import dyn_limit_reranker_docs


def _docs(scores):
    return [{"chunk_id": i, "score": s} for i, s in enumerate(scores)]


def test_empty_input_returns_empty():
    assert dyn_limit_reranker_docs([]) == []


def test_flat_scores_keep_all_up_to_cap():
    docs = _docs([0.9, 0.89, 0.88, 0.87, 0.86])
    assert len(dyn_limit_reranker_docs(docs)) == 5


def test_big_drop_truncates():
    docs = _docs([0.95, 0.94, 0.6, 0.59, 0.58])
    assert [d["chunk_id"] for d in dyn_limit_reranker_docs(docs)] == [0, 1]


def test_min_topk_is_always_kept():
    docs = _docs([0.9, 0.1, 0.05, 0.01])
    assert len(dyn_limit_reranker_docs(docs)) >= 2
