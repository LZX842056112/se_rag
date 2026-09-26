import sys, json
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.infra.vector_store.milvus_gateway import milvus_gateway

client = milvus_gateway.client
for coll in (milvus_gateway.chunk_collection_name,
             milvus_gateway.item_collection_name,
             milvus_gateway.evolution_collection_name):
    try:
        if not client.has_collection(coll):
            print(f"[{coll}] 不存在")
            continue
        schema = client.describe_collection(coll)
        fnames = [f["name"] for f in schema.get("fields", [])]
        idxs = client.list_indexes(coll)
        details = []
        for i in idxs:
            try:
                d = client.describe_index(coll, i)
                details.append(d)
            except Exception as e:
                details.append(f"idx_err({i}): {e}")
        print(f"[{coll}] fields={fnames}")
        print(f"   indexes={json.dumps(details, ensure_ascii=False, default=str)}")
    except Exception as e:
        print(f"[{coll}] ERROR: {e}")