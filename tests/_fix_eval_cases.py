"""用当前 Milvus 实测评测 chunk（按 part 顺序）重建评测用例文件，修复 plan用例与数据错位导致的指标全 0。"""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.infra.vector_store.milvus_gateway import milvus_gateway
from app.rag_eval.dataset import (
    TEST_FILE_TITLE, TEST_ITEM_NAME, GENERATED_BATCH_CASES_FILE,
    build_import_chunks, build_batch_eval_cases, write_batch_eval_cases,
)

client = milvus_gateway.client
rows = client.query(
    collection_name=milvus_gateway.chunk_collection_name,
    filter=f"file_title == '{TEST_FILE_TITLE}'",
    output_fields=["chunk_id", "item_name", "part"],
    limit=16384,
)
expected = len(build_import_chunks())
print(f"live chunks[{TEST_FILE_TITLE}] = {len(rows)}, expected = {expected}")
if len(rows) != expected:
    raise SystemExit(f"实测 chunk 数量({len(rows)})与期望({expected})不一致，请先清理重复评测数据后再重建用例。")

# 按 part 升序取 chunk_id（Milvus AUTO_ID 单调且与导入 part 顺序一致）
rows_sorted = sorted(rows, key=lambda r: (r.get("part", 10**9), str(r.get("chunk_id"))))
gold_ids = [str(r["chunk_id"]) for r in rows_sorted]
print("gold_chunk_ids(part序):", gold_ids)

cases = build_batch_eval_cases(gold_chunk_ids=gold_ids, expected_item_names=[TEST_ITEM_NAME])
write_batch_eval_cases(cases)
print("已写入用例文件:", GENERATED_BATCH_CASES_FILE)
print("case[0].gold_head=", cases[0]["gold_chunk_ids"][:2], "must_hit=", cases[0]["must_hit_chunk_ids"])