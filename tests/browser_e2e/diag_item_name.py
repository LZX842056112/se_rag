"""主体识别链路诊断（联调排查用，测试产物，非产品代码）。

用途：在联调环境内直接复现「主体识别」链路的三段（目录 / LLM 抽取 / 向量判定），
     定位「未关联到任何主体」到底断在哪一段，为报告提供根因证据。

用法：
    python tests/browser_e2e/diag_item_name.py "HAK 180 烫金机 保养维护要点"
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def main() -> None:
    query = sys.argv[1] if len(sys.argv) > 1 else "HAK 180 烫金机 保养维护要点"

    from app.rag.query.item_name_catalog import load_item_names, match_catalog_exact
    from app.rag.query.item_name_confirm_service import (
        call_llm_item_name_and_rewritten,
        search_by_item_names,
        select_item_names,
    )

    print(f"=== query: {query} ===")

    from app.infra.vector_store.milvus_gateway import milvus_gateway

    client = milvus_gateway.milvus_client
    try:
        dbs = list(client.list_databases())
    except Exception as e:  # noqa: BLE001
        dbs = [f"<err {e}>"]
    print(f"[0 milvus] databases={json.dumps(dbs, ensure_ascii=False)}")
    cols = list(client.list_collections())
    print(f"[0 milvus] collections={json.dumps(cols, ensure_ascii=False)}")
    for col in cols:
        try:
            stats = client.get_collection_stats(collection_name=col)
            print(f"[0 milvus] {col}: row_count={stats.get('row_count')}")
        except Exception as e:  # noqa: BLE001
            print(f"[0 milvus] {col}: stats_err={e}")
    print(f"[0 milvus] item_name_collection_name={milvus_gateway.item_name_collection_name} "
          f"chunk_collection_name={getattr(milvus_gateway, 'chunk_collection_name', '?')}")

    names = load_item_names(force=True)
    print(f"[1 catalog] count={len(names)}")
    print(f"[1 catalog] sample={json.dumps(names[:25], ensure_ascii=False)}")
    for probe in ("HAK 180 烫金机", "HAK180", "烫金机"):
        print(f"[1 catalog] exact({probe!r})={match_catalog_exact(probe)!r}")

    result = call_llm_item_name_and_rewritten("", query)
    print(f"[2 llm] {json.dumps(result, ensure_ascii=False)}")

    item_names = result.get("item_names") or []
    if not item_names:
        print("[3 milvus] SKIP -> LLM 未抽取到任何主体（断点在第 2 段）")
        return

    milvus_result = search_by_item_names(item_names)
    for name, hits in milvus_result.items():
        print(f"[3 milvus] {name!r} -> hits={len(hits)} top={json.dumps(hits[:5], ensure_ascii=False)}")

    selected = select_item_names(milvus_result)
    print(f"[4 select] {json.dumps(selected, ensure_ascii=False)}")


if __name__ == "__main__":
    main()
