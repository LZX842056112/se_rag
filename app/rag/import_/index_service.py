"""入库服务：校验切片 → 准备集合 → 清理同文档旧数据 → 批量写入 Milvus，并写入章节级父块。

schema 要点：

- ``chunk_id`` 由自增 INT64 改为**确定性哈希**（``auto_id=False``）——重复导入后 id 稳定，
  绑在 id 上的标注、反馈、引用不会失效；
- 新增 ``doc_id`` / ``parent_id`` / ``heading_path`` / ``seq`` / ``page`` / ``content_hash``；
- ``part`` 由 INT8 升为 INT16（INT8 上限 127，单章节切块数超限会导致插入失败）。

写入前统一经 ``_align_to_schema`` 对齐：Milvus 的 ``insert`` 只认字段名，历史实现无任何
校验，dict 键与 schema 不一致时会静默写入错误数据。
"""
from __future__ import annotations

from pymilvus import DataType

from app.rag.import_.ids import truncate_utf8
from app.rag.import_.parent_index_service import index_parent_chunks
from app.rag.import_.reconcile import reconcile_after_import
from app.shared.clients.milvus_gateway import (
    eq_expr,
    in_expr,
    milvus_gateway,
    register_vector_fields_and_indexes,
)
from app.shared.config import settings
from app.shared.runtime.logger import logger, step_log
from app.shared.utils.require import fail, require_state_list

# 子块集合的 VARCHAR 字段与字节上限（Milvus 的 max_length 按**字节**计）
CHUNK_VARCHAR_LIMITS: dict[str, int] = {
    "chunk_id": 40,
    "doc_id": 64,
    "parent_id": 40,
    "file_title": 512,
    "item_name": 512,
    "title": 512,
    "parent_title": 512,
    "heading_path": 1024,
    "content": 65535,
    "content_hash": 40,
    "document_kind": 16,
    "document_version": 128,
}

# 标量字段与缺省值
CHUNK_SCALAR_DEFAULTS: dict[str, int] = {"part": 1, "seq": 0, "page": 0}

# 向量字段（由 register_vector_fields_and_indexes 统一注册）
CHUNK_VECTOR_FIELDS: tuple[str, ...] = ("dense_vector", "sparse_vector")

# 残留旧块对账：单次查询上限（按 file_title 过滤后的条数远小于该值）
CHUNK_RECONCILE_LIMIT = 16384
# 删除残留旧块时的分批大小
CHUNK_DELETE_BATCH_SIZE = 500


@step_log("prepare_chunks_collection")
def prepare_chunks_collection() -> None:
    """集合不存在时创建 ``kb_chunks``（schema + 索引）。"""
    client = milvus_gateway.milvus_client
    collection_name = milvus_gateway.chunk_collection_name
    if client.has_collection(collection_name=collection_name):
        return

    schema = client.create_schema(auto_id=False, enable_dynamic_field=True)
    for field_name, max_length in CHUNK_VARCHAR_LIMITS.items():
        schema.add_field(
            field_name=field_name,
            datatype=DataType.VARCHAR,
            max_length=max_length,
            is_primary=(field_name == "chunk_id"),
        )
    for field_name in CHUNK_SCALAR_DEFAULTS:
        datatype = DataType.INT16 if field_name == "part" else DataType.INT32
        schema.add_field(field_name=field_name, datatype=datatype)

    schema.add_field(field_name="valid_until", datatype=DataType.DOUBLE)
    index_params = client.prepare_index_params()
    register_vector_fields_and_indexes(schema, index_params, dense_metric="COSINE")
    client.create_collection(collection_name=collection_name, schema=schema, index_params=index_params)
    logger.info(f"已创建知识库集合：{collection_name}")


def _align_to_schema(rows: list[dict]) -> list[dict]:
    """把切片对齐到集合 schema：补齐缺省字段、按字节截断 VARCHAR、剔除多余键。"""
    aligned: list[dict] = []
    unexpected: set[str] = set()

    for index, row in enumerate(rows):
        item: dict = {}
        for field_name, max_length in CHUNK_VARCHAR_LIMITS.items():
            value = row.get(field_name)
            item[field_name] = truncate_utf8("" if value is None else str(value), max_length)
        for field_name, default in CHUNK_SCALAR_DEFAULTS.items():
            value = row.get(field_name)
            item[field_name] = int(value) if isinstance(value, (int, float)) else default
        item["valid_until"] = float(row.get("valid_until") or 0.0)
        for field_name in CHUNK_VECTOR_FIELDS:
            vector = row.get(field_name)
            if not vector:
                fail(field_name, f"缺失（第 {index} 条切片），无法入库")
            item[field_name] = vector
        unexpected.update(key for key in row if key not in item)
        aligned.append(item)

    if unexpected:
        logger.warning(f"切片存在集合 schema 之外的字段，已忽略：{sorted(unexpected)}")
    return aligned


@step_log("batch_upsert_chunks")
def _batch_upsert(client, collection_name: str, rows: list[dict], batch_size: int) -> int:
    """分批 upsert，返回累计写入条数。

    ``chunk_id`` 是确定性哈希主键（``auto_id=False``），因此 upsert 是幂等的：
    同一文件重复导入覆盖同 id，文档新增章节时新 id 被插入。
    """
    total = 0
    for start in range(0, len(rows), batch_size):
        batch = rows[start:start + batch_size]
        result = client.upsert(collection_name=collection_name, data=batch)
        # Milvus upsert 返回 upsert_count；部分版本沿用 insert_count
        total += int(result.get("upsert_count", result.get("insert_count", 0)))
    return total


@step_log("delete_stale_chunks")
def _delete_stale_chunks(client, collection_name: str, file_title: str, new_ids: set[str]) -> int:
    """删除该文档下「本次未再产出」的旧块，返回删除条数。

    为什么必须保留这一步：``chunk_id`` 只编码结构（``doc_key#heading_path#occurrence#part``），
    文档增删章节 / 重命名标题 / 章节内分块数变化时旧 id 不再被产出，纯 upsert 会让它们永久残留。

    查询失败只告警，**绝不回退到「先删后插」**——那会重新引入「删成功、插失败即丢数据」的空窗。
    """
    try:
        existing = client.query(
            collection_name=collection_name,
            filter=eq_expr("file_title", file_title),
            output_fields=["chunk_id"],
            limit=CHUNK_RECONCILE_LIMIT,
        )
    except Exception as exc:  # noqa: BLE001 - 清理失败不得危及已写入的新数据
        logger.warning(f"残留旧块对账查询失败（跳过清理，新数据已安全写入）：{exc}")
        return 0

    stale = [
        str(row.get("chunk_id"))
        for row in (existing or [])
        if row.get("chunk_id") and str(row.get("chunk_id")) not in new_ids
    ]
    for start in range(0, len(stale), CHUNK_DELETE_BATCH_SIZE):
        batch = stale[start:start + CHUNK_DELETE_BATCH_SIZE]
        client.delete(collection_name=collection_name, filter=in_expr("chunk_id", batch))
    return len(stale)


@step_log("index_chunks")
def index_chunks(state: dict) -> dict:
    """入库服务入口：**先写新数据、再清理本次未产出的旧块**（避免数据空窗）。

    历史实现是 ``delete(file_title)`` → ``insert``：删除成功但插入失败会让该文档
    从知识库中彻底消失。现改为 upsert + 残留清理，任何时刻集合内都至少有旧数据或新数据。
    """
    embeddings_content = require_state_list(state, "embeddings_content")
    prepare_chunks_collection()

    file_title = state.get("file_title", "")
    client = milvus_gateway.milvus_client
    collection_name = milvus_gateway.chunk_collection_name

    metadata = {"document_kind": state.get("document_kind", "manual"),
                "document_version": state.get("document_version", ""),
                "valid_until": state.get("valid_until", 0.0)}
    if not metadata['document_version']:
        import hashlib
        import json
        metadata['document_version'] = hashlib.sha256(json.dumps(
            [{k: v for k, v in row.items() if k not in ('dense_vector', 'sparse_vector')}
             for row in embeddings_content], ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()
        state['document_version'] = metadata['document_version']
    rows = _align_to_schema([{**row, **metadata} for row in embeddings_content])
    # 在替换其来源之前，先撤销派生事实。若失败则保持
    # 撤销状态，直到审核人员重新校验；过期事实绝不能泄漏。
    from app.evolution.governance import invalidate_document
    from app.evolution.flags import recall_enabled, writes_enabled
    if recall_enabled() or writes_enabled():
        for source_doc_id in {str(row.get('doc_id')) for row in rows if row.get('doc_id')}:
            invalidate_document(source_doc_id)
    new_ids = {str(row["chunk_id"]) for row in rows}

    # 1) 先写：分批 upsert
    batch_size = max(1, settings.import_.milvus_insert_batch_size)
    written = _batch_upsert(client, collection_name, rows, batch_size)
    logger.info(f"知识库写入完成：upsert_count={written}（分片大小 {batch_size}）")

    # 2) 对账：写入条数与期望不符即失败，避免「静默少写」被当成成功
    if written != len(rows):
        fail("insert_count", f"对账失败：写入 {written} 条，期望 {len(rows)} 条")

    # 3) 后清：删除本次未再产出的旧块（章节被删除/重命名时产生）
    if file_title:
        removed = _delete_stale_chunks(client, collection_name, file_title, new_ids)
        if removed:
            logger.info(f"已清理 {file_title} 的残留旧块 {removed} 条")

    index_parent_chunks(state)
    reconcile_after_import(state)
    return state