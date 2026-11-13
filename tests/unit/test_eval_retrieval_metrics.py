"""检索指标回归：排序、截断与无标注场景。"""
from __future__ import annotations

import pytest

from app.rag_eval.dataset import EvalCase
from app.rag_eval.retrieval_metrics import (
    aggregate,
    evaluate_case,
    mrr_at_k,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
)


def test_precision_and_recall_respect_k():
    retrieved = ["a", "b", "c", "d"]
    gold = ["b", "d"]
    assert precision_at_k(retrieved, gold, 2) == 0.5
    assert precision_at_k(retrieved, gold, 4) == 0.5
    assert recall_at_k(retrieved, gold, 2) == 0.5
    assert recall_at_k(retrieved, gold, 4) == 1.0


def test_mrr_uses_first_hit_rank():
    assert mrr_at_k(["x", "b", "c"], ["b"], 3) == 0.5
    assert mrr_at_k(["x", "y", "z"], ["b"], 3) == 0.0


def test_ndcg_prefers_relevant_items_at_top():
    gold = ["a", "b"]
    top = ndcg_at_k(["a", "b", "x"], gold, 3)
    bottom = ndcg_at_k(["x", "a", "b"], gold, 3)
    assert top == 1.0
    assert 0.0 < bottom < top


def test_metrics_return_none_without_gold():
    assert precision_at_k(["a"], [], 3) is None
    assert recall_at_k(["a"], [], 3) is None
    assert mrr_at_k(["a"], [], 3) is None
    assert ndcg_at_k(["a"], [], 3) is None


def test_evaluate_case_and_aggregate_layers():
    case = EvalCase(
        case_id="c1",
        question="q",
        ground_truth="g",
        gold_chunk_ids=("a", "b"),
        must_hit_chunk_ids=("a",),
    )
    layers = {
        "embedding": ["a", "x"],
        "hyde": [],
        "rrf": ["x", "b"],
        "reranked": ["a", "b"],
    }
    result = evaluate_case(layers, case, (1, 2))
    assert result["reranked"]["recall@2"] == 1.0
    assert result["reranked"]["must_hit@2"] == 1.0
    assert result["embedding"]["precision@2"] == 0.5
    assert result["hyde"]["recall@2"] == 0.0

    summary = aggregate([{"retrieval": result}], (1, 2))
    assert summary["reranked"]["recall@2"] == 1.0
    assert summary["reranked"]["recall@2_n"] == 1
    assert "recall@2" in summary["embedding"]


@pytest.mark.parametrize("k", [1, 3, 5])
def test_ndcg_in_valid_range(k):
    value = ndcg_at_k(["a", "b", "c"], ["b"], k)
    assert 0.0 <= value <= 1.0
