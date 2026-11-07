"""检索排序指标：Recall@k、MRR、NDCG@k。"""
import pytest


def test_recall_at_k_mrr_and_ndcg_use_ranked_order():
    from app.rag_eval.metrics import compute_recall_at_k, compute_mrr, compute_ndcg_at_k
    retrieved = ["a", "b", "c"]
    gold = ["b", "c"]
    assert compute_recall_at_k(retrieved, gold, 1) == 0.0
    assert compute_recall_at_k(retrieved, gold, 2) == 0.5
    assert compute_recall_at_k(retrieved, gold, 3) == 1.0
    assert compute_mrr(retrieved, gold) == 0.5
    assert compute_ndcg_at_k(retrieved, gold, 2) == pytest.approx(0.3868528, abs=1e-6)
    assert compute_ndcg_at_k(retrieved, gold, 3) == pytest.approx(0.6934264, abs=1e-6)


def test_ranking_metrics_handle_empty_and_missing_gold():
    from app.rag_eval.metrics import compute_recall_at_k, compute_mrr, compute_ndcg_at_k
    assert compute_recall_at_k([], ["a"], 5) == 0.0
    assert compute_mrr(["a"], []) == 0.0
    assert compute_ndcg_at_k(["a"], [], 5) == 0.0
    assert compute_recall_at_k(["a"], ["a"], 0) == 0.0
    assert compute_mrr(["x", "y"], ["z"]) == 0.0


def test_chunk_metrics_include_recall_mrr_and_ndcg_and_deduplicate():
    from app.rag_eval.metrics import compute_chunk_metrics
    result = compute_chunk_metrics(["a", "a", "b"], ["b", "c"], ["b"], k_values=(3,))
    assert result["retrieved_chunk_ids"] == ["a", "b"]
    assert result["precision"] == 0.5
    assert result["recall"] == 0.5
    assert result["must_hit_rate"] == 1.0
    assert result["recall_at_3"] == 0.5
    assert result["mrr"] == 0.5
    assert result["ndcg_at_3"] == pytest.approx(0.3869, abs=1e-4)
