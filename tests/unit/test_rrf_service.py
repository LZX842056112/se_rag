"""RRF 融合测试：权重打分、跨路去重、Top-N 截断与无 id 结果跳过。"""
from __future__ import annotations

from app.rag.query.rrf_service import use_by_rrf


def _chunk(cid, content="x"):
    return {"chunk_id": cid, "content": content, "source": "milvus"}


def test_rrf_scores_and_orders_by_rank():
    path_a = [_chunk(1), _chunk(2), _chunk(3)]
    path_b = [_chunk(3), _chunk(4)]
    fused = use_by_rrf([(1.0, path_a), (1.0, path_b)], top=10, k=60)
    ids = [c["chunk_id"] for c in fused]
    # 3 在两路都命中，得分最高
    assert ids[0] == 3
    assert set(ids) == {1, 2, 3, 4}


def test_rrf_respects_weights():
    path_a = [_chunk(1)]
    path_b = [_chunk(2)]
    fused = use_by_rrf([(5.0, path_a), (0.1, path_b)], top=10, k=60)
    assert [c["chunk_id"] for c in fused] == [1, 2]


def test_rrf_truncates_to_top():
    fused = use_by_rrf([(1.0, [_chunk(i) for i in range(1, 11)])], top=3, k=60)
    assert len(fused) == 3


def test_rrf_skips_chunks_without_id():
    fused = use_by_rrf([(1.0, [{"content": "no id"}, _chunk(9)])], top=5, k=60)
    assert [c["chunk_id"] for c in fused] == [9]


def test_rrf_replaces_score_with_fusion_score():
    fused = use_by_rrf([(1.0, [_chunk(1)])], top=1, k=60)
    assert fused[0]["score"] == 1.0 / 61
