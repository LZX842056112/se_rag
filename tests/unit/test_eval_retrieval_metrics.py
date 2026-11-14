"""检索指标回归：排序、截断与无标注场景。"""
from __future__ import annotations

import pytest

from app.rag_eval.dataset import EvalCase
from app.rag_eval.retrieval_metrics import (
    aggregate,
    evaluate_case,
    mrr_at_k,
    must_hit_rate,
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


def test_retrieval_metrics_count_duplicate_hits_once_without_promoting_later_ranks():
    retrieved = ["a", "a", "b"]
    gold = ["a", "b"]
    assert precision_at_k(retrieved, gold, 5) == 0.4
    assert recall_at_k(retrieved, gold, 2) == 0.5
    assert must_hit_rate(retrieved, gold, 2) == 0.5
    assert ndcg_at_k(retrieved, gold, 3) < 1


def test_equivalent_evidence_preserves_rank_and_requires_distinct_facts():
    case = EvalCase.from_dict({
        "case_id": "c1", "question": "q", "ground_truth": "g",
        "gold_chunk_ids": ["a", "b"], "must_hit_chunk_ids": ["a", "b"],
        "chunk_equivalents": {"a": ["another-a"]},
    })
    metrics = evaluate_case({"final": ["another-a", "a", "b"]}, case, (2, 3))["final"]
    assert metrics["mrr@2"] == 1
    assert metrics["precision@2"] == 0.5
    assert metrics["must_hit@2"] == 0.5
    assert metrics["recall@3"] == 1
    assert metrics["must_hit@3"] == 1


def test_skipped_layer_has_no_score_but_executed_empty_layer_scores_zero():
    case = EvalCase("c1", "q", "g", gold_chunk_ids=("a",))
    scored = evaluate_case({"agentic_retrieval": ["a"], "final": []}, case, (5,))
    summary = aggregate([{"retrieval": scored}], (5,))
    assert "reranked" not in scored
    assert "reranked" not in summary
    assert summary["agentic_retrieval"]["recall@5"] == 1
    assert summary["final"]["recall@5"] == 0
