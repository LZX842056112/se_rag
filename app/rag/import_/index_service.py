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
from app.shared.clients.milvus_gateway import (
    eq_expr,
    milvus_gateway,
    register_vector_fields_and_indexes,
)
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
}

# 标量字段与缺省值
CHUNK_SCALAR_DEFAULTS: dict[str, int] = {"part": 1, "seq": 0, "page": 0}

# 向量字段（由 register_vector_fields_and_indexes 统一注册）
CHUNK_VECTOR_FIELDS: tuple[str, ...] = ("dense_vector", "sparse_vector")


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


@step_log("index_chunks")
def index_chunks(state: dict) -> dict:
    """入库服务入口：同 ``file_title`` 重复导入时先删旧数据再写新数据（幂等覆盖）。"""
    embeddings_content = require_state_list(state, "embeddings_content")
    prepare_chunks_collection()

    file_title = state.get("file_title", "")
    client = milvus_gateway.milvus_client
    collection_name = milvus_gateway.chunk_collection_name
    if file_title:
        client.delete(collection_name=collection_name, filter=eq_expr("file_title", file_title))

    result = client.insert(collection_name=collection_name, data=_align_to_schema(embeddings_content))
    logger.info(f"知识库写入完成：insert_count={result.get('insert_count', 0)}")

    index_parent_chunks(state)
    return state
