import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.infra.vector_store.milvus_gateway import milvus_gateway
from app.rag_eval.dataset import TEST_FILE_TITLE

client = milvus_gateway.client
rows = client.query(collection_name=milvus_gateway.chunk_collection_name,
                    filter=f"file_title == '{TEST_FILE_TITLE}'",
                    output_fields=["chunk_id", "item_name", "vector", "content"], limit=16384)
print("num rows =", len(rows))
if rows:
    for r in rows[:2]:
        vec = r.get("vector")
        print("ckid=", r.get("chunk_id"), "item_name=", r.get("item_name"),
              "vec_type=", type(vec).__name__,
              "vec_len=", (len(vec) if vec else 0))

# 尝试一次混合检索
import app.rag.query.embedding_search_service as ess
from app.process.query.agent.state import create_query_default_state
st = create_query_default_state(session_id="probe", original_query="局部烫印 50mm 170mm 区域 设置",
                                rewritten_query="HAK 180 在局部烫印时怎么设置 50mm 到 170mm 区域",
                                item_names=["HAK 180"], is_stream=False, web_search_docs=[])
res = ess.search_by_embedding(st)
print("search_by_embedding returned", len(res.get("embedding_chunks", []) if isinstance(res, dict) else res))
if isinstance(res, dict):
    for c in res.get("embedding_chunks", [])[:5]:
        print("  hit=", c.get("chunk_id"), c.get("item_name"), c.get("score"))
# 直接看 search 的 expr 与 collection
print("collection=", milvus_gateway.chunk_collection_name)