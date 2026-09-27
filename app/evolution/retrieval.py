"""
进化条目召回：查询 kb_evolution_items（仅 active），供 RRF 融合。
失败返回空列表，绝不阻断主链路。结果形状对齐 embedding_search 的 embedding_chunks。
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any

from app.infra.llm.providers import llm_providers
from app.infra.vector_store.milvus_gateway import milvus_gateway
from app.shared.runtime.logger import logger
from app.shared.utils.escape_milvus_string_utils import escape_milvus_string

# 未打商品标签的候选（写入端 item_name 为空时落为占位符）视为全局可召回
_GLOBAL_ITEM = "default_item_name"


def _name_tokens(name: object) -> list[str]:
    """主体名切分：NFKC 归一 + 小写 + 按空白切 token（保留 token 边界，供前缀等价判定）。"""
    return unicodedata.normalize("NFKC", str(name or "")).lower().split()


def _token_prefix_match(a: list[str], b: list[str]) -> bool:
    """较短 token 序列是较长者的前缀即视为等价（短边在前，兼容 `HAK 180` vs `HAK 180 烫金机`）。"""
    if not a or not b:
        return False
    m = min(len(a), len(b))
    return a[:m] == b[:m]


def _name_equivalent(stored: object, query_names: list[str]) -> bool:
    """主体名等价判定：占位符全局可召回；否则逐主体与查询名做 token 前缀等价。

    写入端多主体用逗号连接（见 ``app/evolution/index/update.py::_canonical_subject``），
    因此必须先按逗号拆成单个主体再判定；否则 `HAK 180 烫金机,Brother HAK 180 烫金机`
    会切出 `烫金机,brother` 这种跨主体 token，前缀判定永远不成立、已审批条目召回不到。

    例：`HAK 180` -> ["hak","180"] 是 `HAK 180 烫金机` -> ["hak","180","烫金机"] 的前缀 => 等价；
    而 `HAK 180` 与 `HAK 1800` -> ["hak","1800"] 不构成前缀 => 不等价，避免尾号误判。
    """
    text = str(stored or "").strip()
    if text == _GLOBAL_ITEM:
        return True
    subjects = [s for s in re.split(r"[,，]", text) if s.strip()]
    query_tokens = [_name_tokens(n) for n in query_names]
    for subject in subjects:
        st = _name_tokens(subject)
        if any(_token_prefix_match(st, qt) for qt in query_tokens):
            return True
    return False


def _search(rewritten_query: str, expr: str, limit: int) -> list[dict[str, Any]]:
    """按给定过滤表达式做一次混合检索并格式化。"""
    result = llm_providers.generate_embeddings([rewritten_query])
    reqs = milvus_gateway.create_requests(
        dense_vector=result["dense"][0],
        sparse_vector=result["sparse"][0],
        expr=expr,
        limit=max(limit * 2, 10),
    )
    milvus_result = milvus_gateway.hybrid_search(
        collection_name=milvus_gateway.evolution_collection_name,
        reqs=reqs,
        ranker_weights=(0.6, 0.4),
        norm_score=True,
        limit=limit,
        output_fields=[
            "evo_doc_id", "faq_question", "faq_answer", "source_refs", "item_name", "status"
        ],
    )
    raw_list = milvus_result[0] if milvus_result and len(milvus_result) > 0 else []
    return _format(raw_list)


def search_evolution_items(rewritten_query: str, item_names: list[str], limit: int = 10) -> list[dict[str, Any]]:
    if not rewritten_query:
        return []
    try:
        # 0. 集合可能尚未创建（无已审批 active 候选），跳过演进召回，避免每次检索刷 collection not found
        if not milvus_gateway.milvus_client.has_collection(collection_name=milvus_gateway.evolution_collection_name):
            return []
        # 1. 带商品名：仅召回属于当前商品的候选 + 未打标签的全局候选（逐项转义，防 filter 注入）
        if item_names:
            in_list = ", ".join(f"'{escape_milvus_string(n)}'" for n in item_names)
            expr = f"status == 'active' and (item_name in [{in_list}] or item_name == '{_GLOBAL_ITEM}')"
            hits = _search(rewritten_query, expr, limit)
            if hits:
                return hits
            # 2. 主体名口径不一致（别名/近重复名，如 HAK 180 vs HAK 180 烫金机）兜底：
            #    放开商品过滤重查，再按 token 前缀等价在本地裁决，避免跨商品误召回
            wide = _search(rewritten_query, "status == 'active'", max(limit * 4, 40))
            return [h for h in wide if _name_equivalent(h.get("item_name"), item_names)][:limit]
        # 3. 无商品名：全局召回 active
        return _search(rewritten_query, "status == 'active'", limit)
    except Exception as e:
        logger.warning(f"进化条目召回失败，返回空: {e}")
        return []


def _format(milvus_list: list[dict]) -> list[dict[str, Any]]:
    items = []
    for item in milvus_list:
        entity = item.get("entity", {})
        items.append({
            "chunk_id": entity.get("evo_doc_id"),
            "score": item.get("distance", 0.0),
            "title": entity.get("faq_question"),
            "file_title": "__evolution__",
            "parent_title": "",
            "part": 0,
            "item_name": entity.get("item_name"),
            "content": entity.get("faq_answer"),
            "source": "evolution",
            "url": "",
            "type": "milvus",
        })
    return items
