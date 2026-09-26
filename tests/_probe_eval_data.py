import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.infra.vector_store.milvus_gateway import milvus_gateway
from app.rag_eval.dataset import TEST_FILE_TITLE, TEST_ITEM_NAME, load_batch_eval_cases

client = milvus_gateway.client
# chunks 集合中该题主体数量
rows = client.query(collection_name=milvus_gateway.chunk_collection_name,
                    filter=f"file_title == '{TEST_FILE_TITLE}'",
                    output_fields=["chunk_id", "item_name"], limit=16384)
print(f"chunks[{TEST_FILE_TITLE}] count = {len(rows)}")
item_rows = client.query(collection_name=milvus_gateway.item_collection_name,
                         filter=f"file_title == '{TEST_FILE_TITLE}'",
                         output_fields=["item_name","file_title"], limit=16384)
print(f"items[{TEST_FILE_TITLE}] = {len(item_rows)}")
cases = load_batch_eval_cases()
print(f"eval cases = {len(cases)}")
if cases:
    c = cases[0]
    print("case[0] item_names=", c.get("expected_item_names"))
    print("case[0] gold_chunk_ids=", c.get("gold_chunk_ids"))
    print("case[0] must_hit=", c.get("must_hit_chunk_ids"))
# 全库 item 是否有任何记录（判断集合是否为空）
all_items = client.query(collection_name=milvus_gateway.item_collection_name,
                         output_fields=["item_name","file_title"], limit=10)
print("sample items in item_collection:", all_items)