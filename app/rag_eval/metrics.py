"""
评估指标模块。

这个文件只做“怎么算分”。

读这个文件时可以抓住 7 个指标：
1. `item_name_hit_rate`：主体识别对不对；
2. `precision`：检索出来的内容准不准；
3. `recall`：该召回的内容有没有召回到；
4. `must_hit_rate`：关键 chunk 有没有打中；
5. `recall_at_k`：前 k 条里召回了多少相关 chunk；
6. `mrr`：第一个相关 chunk 的平均倒数排名；
7. `ndcg_at_k`：前 k 条的排序质量。
"""

import math
from typing import Any


def _normalize_chunk_ids(chunk_ids: list[Any] | None) -> list[str]:
    """
    将各种类型的 chunk_id 统一转成字符串。

    参数：
    - chunk_ids：原始 chunk_id 列表，里面可能混有 int / str / None

    返回值：
    - list[str]：统一转成字符串后的 id 列表

    这样后面做集合比较时，不会因为 int / str 混用而出错。
    """
    if not chunk_ids:
        return []
    return [str(chunk_id) for chunk_id in chunk_ids if chunk_id is not None]


def _deduplicate_keep_order(values: list[str]) -> list[str]:
    """
    去重但保留原始顺序。

    参数：
    - values：原始字符串列表

    返回值：
    - list[str]：去重后的列表

    这里不用 set 直接去重，是因为检索结果的顺序本身也有意义。
    """
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def extract_chunk_ids(docs: list[dict] | None) -> list[str]:
    """
    从一层检索结果中提取 chunk_id 列表。

    参数：
    - docs：某一层的检索结果列表

    返回值：
    - list[str]：当前层检索结果对应的 chunk_id 列表

    当前项目每一层结果都约定用 `chunk_id` 标识文档，
    所以这里统一提取，供后面的指标计算复用。
    """
    if not docs:
        return []
    chunk_ids: list[str] = []
    for doc in docs:
        chunk_id = doc.get("chunk_id")
        if chunk_id is None:
            continue
        chunk_ids.append(str(chunk_id))
    return _deduplicate_keep_order(chunk_ids)


def compute_item_name_hit_rate(
    predicted_item_names: list[str] | None,
    expected_item_names: list[str] | None,
) -> float:
    """
    计算主体识别命中率。

    参数：
    - predicted_item_names：模型或流程最终识别出的主体列表
    - expected_item_names：题库里标注的预期主体列表

    返回值：
    - float：主体命中率

    公式：
    预测主体和预期主体的交集数量 / 预期主体数量
    """
    predicted = set(predicted_item_names or [])
    expected = set(expected_item_names or [])
    if not expected:
        return 0.0
    return len(predicted & expected) / len(expected)


def compute_recall_at_k(
    retrieved_chunk_ids: list[Any] | None,
    gold_chunk_ids: list[Any] | None,
    k: int,
) -> float:
    """计算 Recall@k：前 k 条中命中的相关 chunk 数 / 全部相关 chunk 数。"""
    retrieved = _deduplicate_keep_order(_normalize_chunk_ids(retrieved_chunk_ids))
    gold = set(_deduplicate_keep_order(_normalize_chunk_ids(gold_chunk_ids)))
    if not gold or k <= 0:
        return 0.0
    return len(set(retrieved[:k]) & gold) / len(gold)


def compute_mrr(
    retrieved_chunk_ids: list[Any] | None,
    gold_chunk_ids: list[Any] | None,
) -> float:
    """计算 MRR：第一个相关 chunk 的排名倒数；没有命中返回 0。"""
    retrieved = _deduplicate_keep_order(_normalize_chunk_ids(retrieved_chunk_ids))
    gold = set(_deduplicate_keep_order(_normalize_chunk_ids(gold_chunk_ids)))
    if not gold:
        return 0.0
    for rank, chunk_id in enumerate(retrieved, start=1):
        if chunk_id in gold:
            return 1.0 / rank
    return 0.0


def compute_ndcg_at_k(
    retrieved_chunk_ids: list[Any] | None,
    gold_chunk_ids: list[Any] | None,
    k: int,
) -> float:
    """计算 NDCG@k：二值相关性下的归一化折损累计增益。"""
    retrieved = _deduplicate_keep_order(_normalize_chunk_ids(retrieved_chunk_ids))
    gold = set(_deduplicate_keep_order(_normalize_chunk_ids(gold_chunk_ids)))
    if not gold or k <= 0:
        return 0.0
    dcg = sum(1.0 / math.log2(rank + 1)
              for rank, chunk_id in enumerate(retrieved[:k], start=1) if chunk_id in gold)
    ideal = sum(1.0 / math.log2(rank + 1)
                for rank in range(1, min(len(gold), k) + 1))
    return dcg / ideal if ideal else 0.0


def compute_chunk_metrics(
    retrieved_chunk_ids: list[Any] | None,
    gold_chunk_ids: list[Any] | None,
    must_hit_chunk_ids: list[Any] | None = None,
    *,
    k_values: tuple[int, ...] = (5, 10),
) -> dict[str, Any]:
    """
    计算单层结果的检索指标。

    参数：
    - retrieved_chunk_ids：这一层实际检索到的 chunk_id
    - gold_chunk_ids：题库标注的相关 chunk_id
    - must_hit_chunk_ids：题库标注的关键 chunk_id
    - k_values：Recall@k 与 NDCG@k 的 k 取值

    返回值：
    - dict：包含命中列表、precision、recall、must_hit_rate、Recall@k、MRR、NDCG@k
    """
    retrieved = _deduplicate_keep_order(_normalize_chunk_ids(retrieved_chunk_ids))
    gold = _deduplicate_keep_order(_normalize_chunk_ids(gold_chunk_ids))
    must_hit = _deduplicate_keep_order(_normalize_chunk_ids(must_hit_chunk_ids))

    gold_set = set(gold)
    must_hit_set = set(must_hit)
    hit_chunk_ids = [chunk_id for chunk_id in retrieved if chunk_id in gold_set]
    must_hit_ids = [chunk_id for chunk_id in retrieved if chunk_id in must_hit_set]

    precision = len(hit_chunk_ids) / len(retrieved) if retrieved else 0.0
    recall = len(hit_chunk_ids) / len(gold_set) if gold_set else 0.0
    must_hit_rate = len(must_hit_ids) / len(must_hit_set) if must_hit_set else 0.0

    result = {
        # 这一层实际检索到的 chunk_id 列表。
        # 例如：["101", "102", "103"]
        "retrieved_chunk_ids": retrieved,
        # 这一层一共检索到了多少条结果。
        "retrieved_count": len(retrieved),
        # 题库里标注为“相关答案范围”的 chunk_id 列表。
        "gold_chunk_ids": gold,
        # 题库里一共配置了多少条相关 chunk。
        "gold_count": len(gold),
        # 这一层真正命中的相关 chunk_id。
        # 计算方式：retrieved_chunk_ids 和 gold_chunk_ids 的交集。
        "hit_chunk_ids": hit_chunk_ids,
        # 命中了多少条相关 chunk。
        "hit_count": len(hit_chunk_ids),
        # 题库里标注为“必须命中”的关键 chunk_id 列表。
        "must_hit_chunk_ids": must_hit,
        # 这一层真正命中的关键 chunk_id。
        # 计算方式：retrieved_chunk_ids 和 must_hit_chunk_ids 的交集。
        "must_hit_ids": must_hit_ids,
        # 命中了多少条关键 chunk。
        "must_hit_count": len(must_hit_ids),
        # 精确率：
        # 命中的相关 chunk 数 / 实际检索结果总数。
        # 反映“检索出来的内容准不准”。
        "precision": round(precision, 4),
        # 召回率：
        # 命中的相关 chunk 数 / 题库标注相关 chunk 总数。
        # 反映“该召回的内容有没有召回到”。
        "recall": round(recall, 4),
        # 必命中率：
        # 命中的关键 chunk 数 / 题库标注关键 chunk 总数。
        # 反映“最关键的内容有没有打中”。
        "must_hit_rate": round(must_hit_rate, 4),
    }
    for k in k_values:
        result[f"recall_at_{k}"] = round(compute_recall_at_k(retrieved, gold, k), 4)
        result[f"ndcg_at_{k}"] = round(compute_ndcg_at_k(retrieved, gold, k), 4)
    result["mrr"] = round(compute_mrr(retrieved, gold), 4)
    return result


def evaluate_query_state(result_state: dict[str, Any], expected: dict[str, Any]) -> dict[str, Any]:
    """
    将一次完整查询链路的 state 与标注答案进行对比。

    参数：
    - result_state：当前题目跑完整条查询链路后的 state
    - expected：题库里这道题的标注数据

    返回值：
    - dict：这道题的完整评测结果

    这里固定评估 4 层结果：
    - `embedding_chunks`
    - `hyde_embedding_chunks`
    - `rrf_chunks`
    - `reranked_docs`

    这样最后能看到每一层到底是谁拖了后腿：
    是基础召回差，还是融合后掉了，还是 rerank 选错了。
    """
    expected_item_names = expected.get("expected_item_names", [])
    gold_chunk_ids = expected.get("gold_chunk_ids", [])
    must_hit_chunk_ids = expected.get("must_hit_chunk_ids", [])

    layers = {}
    for layer_name in ("embedding_chunks", "hyde_embedding_chunks", "rrf_chunks", "reranked_docs"):
        layers[layer_name] = compute_chunk_metrics(
            retrieved_chunk_ids=extract_chunk_ids(result_state.get(layer_name, [])),
            gold_chunk_ids=gold_chunk_ids,
            must_hit_chunk_ids=must_hit_chunk_ids,
        )

    return {
        # 当前题目的唯一标识，便于后面查报告或定位问题。
        "case_id": expected.get("case_id", ""),
        # 当前评测问题文本。
        "question": expected.get("question", ""),
        # 题库里配置的预期主体列表。
        "expected_item_names": expected_item_names,
        # 当前流程最终识别出来的主体列表。
        "predicted_item_names": result_state.get("item_names", []),
        # 主体命中率：
        # 识别主体和预期主体的交集数量 / 预期主体数量。
        "item_name_hit_rate": round(
            compute_item_name_hit_rate(result_state.get("item_names", []), expected_item_names),
            4,
        ),
        # 4 层结果的分层评测详情：
        # - embedding_chunks
        # - hyde_embedding_chunks
        # - rrf_chunks
        # - reranked_docs
        "layers": layers,
    }
