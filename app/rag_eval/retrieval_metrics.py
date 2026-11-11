"""检索指标：Precision@k、Recall@k、MRR@k、NDCG@k、必命中率。

输入是「按检索顺序排列的 chunk_id 列表」与标注的 gold / must_hit。
所有指标按 k 截断；gold 为空时返回 ``None``，由聚合层跳过，不按 0 分误伤。
"""
from __future__ import annotations

import math
from typing import Iterable

from app.rag_eval.dataset import EvalCase

# 参与聚合的检索层：与查询图 state 的字段一一对应
LAYERS: tuple[str, ...] = ("embedding", "hyde", "rrf", "reranked")


def _ids(values: Iterable[object] | None) -> list[str]:
    return [str(value) for value in (values or []) if value is not None]


def _hit_flags(retrieved: list[str], gold: set[str], k: int) -> list[int]:
    return [1 if chunk_id in gold else 0 for chunk_id in retrieved[:k]]


def precision_at_k(retrieved: list[str] | None, gold: list[str] | None, k: int) -> float | None:
    """前 k 条里命中的比例；没有检索结果时返回 0.0，没有标注时返回 None。"""
    gold_set = set(_ids(gold))
    if not gold_set:
        return None
    ids = _ids(retrieved)[:k]
    if not ids:
        return 0.0
    return sum(1 for chunk_id in ids if chunk_id in gold_set) / len(ids)


def recall_at_k(retrieved: list[str] | None, gold: list[str] | None, k: int) -> float | None:
    """前 k 条覆盖了多少标注相关 chunk。"""
    gold_set = set(_ids(gold))
    if not gold_set:
        return None
    hits = sum(1 for chunk_id in _ids(retrieved)[:k] if chunk_id in gold_set)
    return hits / len(gold_set)


def mrr_at_k(retrieved: list[str] | None, gold: list[str] | None, k: int) -> float | None:
    """第一条命中所在位置的倒数；未命中为 0。"""
    gold_set = set(_ids(gold))
    if not gold_set:
        return None
    for rank, chunk_id in enumerate(_ids(retrieved)[:k], start=1):
        if chunk_id in gold_set:
            return 1.0 / rank
    return 0.0


def ndcg_at_k(retrieved: list[str] | None, gold: list[str] | None, k: int) -> float | None:
    """二值相关度的 NDCG@k。"""
    gold_set = set(_ids(gold))
    if not gold_set:
        return None
    flags = _hit_flags(_ids(retrieved), gold_set, k)
    dcg = sum(flag / math.log2(index + 2) for index, flag in enumerate(flags))
    ideal_hits = min(len(gold_set), k)
    idcg = sum(1 / math.log2(index + 2) for index in range(ideal_hits))
    return dcg / idcg if idcg else 0.0


def must_hit_rate(retrieved: list[str] | None, must_hit: list[str] | None, k: int) -> float | None:
    """关键 chunk 的命中比例；没有标注时返回 None。"""
    must_set = set(_ids(must_hit))
    if not must_set:
        return None
    hits = sum(1 for chunk_id in _ids(retrieved)[:k] if chunk_id in must_set)
    return hits / len(must_set)


def evaluate_case(result_layers: dict[str, list[str]], case: EvalCase, ks: tuple[int, ...]) -> dict:
    """计算单题、分层、多 k 的检索指标。"""
    layers: dict[str, dict] = {}
    for layer in LAYERS:
        retrieved = _ids(result_layers.get(layer))
        metrics: dict[str, float | None] = {}
        for k in ks:
            prefix = f"@{k}"
            metrics[f"precision{prefix}"] = _round(precision_at_k(retrieved, list(case.gold_chunk_ids), k))
            metrics[f"recall{prefix}"] = _round(recall_at_k(retrieved, list(case.gold_chunk_ids), k))
            metrics[f"mrr{prefix}"] = _round(mrr_at_k(retrieved, list(case.gold_chunk_ids), k))
            metrics[f"ndcg{prefix}"] = _round(ndcg_at_k(retrieved, list(case.gold_chunk_ids), k))
            metrics[f"must_hit{prefix}"] = _round(must_hit_rate(retrieved, list(case.must_hit_chunk_ids), k))
        layers[layer] = metrics
    return layers


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 4)


def aggregate(case_rows: list[dict], ks: tuple[int, ...]) -> dict:
    """按层与指标做宏平均；``None`` 不参与平均。"""
    summary: dict[str, dict[str, float | int]] = {}
    for layer in LAYERS:
        metrics: dict[str, float | int] = {}
        for k in ks:
            for name in ("precision", "recall", "mrr", "ndcg", "must_hit"):
                key = f"{name}@{k}"
                values = [
                    row["retrieval"][layer][key]
                    for row in case_rows
                    if row.get("retrieval") and row["retrieval"].get(layer, {}).get(key) is not None
                ]
                if values:
                    metrics[key] = round(sum(values) / len(values), 4)
                    metrics[f"{key}_n"] = len(values)
        summary[layer] = metrics
    return summary
