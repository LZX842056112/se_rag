"""演进条目召回诊断（联调排查用，测试产物，非产品代码）。

用途：dump kb_evolution_items 全量行，并复现 search_evolution_items 在不同
     item_names 入参下的召回差异，定位「已审批 active 候选未被客服命中」的根因。

用法：
    python tests/browser_e2e/diag_evolution.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def main() -> None:
    from app.infra.vector_store.milvus_gateway import milvus_gateway
    from app.evolution.retrieval import search_evolution_items

    col = milvus_gateway.evolution_collection_name
    client = milvus_gateway.milvus_client
    print(f"[0] evolution_collection={col}")
    if not client.has_collection(collection_name=col):
        print("[0] collection NOT FOUND")
        return
    rows = client.query(
        collection_name=col,
        filter="",
        output_fields=["evo_doc_id", "faq_question", "item_name", "status", "source_refs"],
        limit=100,
    )
    print(f"[1] rows={len(rows)}")
    for r in rows:
        print("[1] " + json.dumps(r, ensure_ascii=False, default=str))

    rewritten = "HAK 180 烫金机的烫印温度与压力调试参数是什么？"
    for names in ([], ["HAK 180 烫金机"], ["HAK180"]):
        hits = search_evolution_items(rewritten, names, limit=10)
        print(f"[2] item_names={json.dumps(names, ensure_ascii=False)} -> hits={len(hits)} "
              f"{json.dumps([{k: h.get(k) for k in ('chunk_id', 'item_name', 'title')} for h in hits], ensure_ascii=False)}")


if __name__ == "__main__":
    main()
