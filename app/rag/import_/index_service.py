"""入库服务：校验切片 → 准备集合 → 清理同文档旧数据 → 批量写入 Milvus。"""
from __future__ import annotations

from pymilvus import DataType

from app.shared.clients.milvus_gateway import (
    eq_expr,
    milvus_gateway,
    register_vector_fields_and_indexes,
)
from app.shared.runtime.logger import logger, step_log
from app.shared.utils.require import require_state_list


@step_log("prepare_chunks_collection")
def prepare_chunks_collection() -> None:
    """集合不存在时创建 ``kb_chunks``（schema + 索引）。"""
    client = milvus_gateway.milvus_client
    collection_name = milvus_gateway.chunk_collection_name
    if client.has_collection(collection_name=collection_name):
        return

    schema = client.create_schema(auto_id=True, enable_dynamic_field=True)
    schema.add_field(field_name="chunk_id", datatype=DataType.INT64, is_primary=True, auto_id=True)
    schema.add_field(field_name="file_title", datatype=DataType.VARCHAR, max_length=512)
    schema.add_field(field_name="item_name", datatype=DataType.VARCHAR, max_length=512)
    schema.add_field(field_name="title", datatype=DataType.VARCHAR, max_length=512)
    schema.add_field(field_name="parent_title", datatype=DataType.VARCHAR, max_length=512)
    schema.add_field(field_name="part", datatype=DataType.INT8)
    schema.add_field(field_name="content", datatype=DataType.VARCHAR, max_length=65535)

    index_params = client.prepare_index_params()
    register_vector_fields_and_indexes(schema, index_params, dense_metric="COSINE")
    client.create_collection(collection_name=collection_name, schema=schema, index_params=index_params)
    logger.info(f"已创建知识库集合：{collection_name}")


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

    result = client.insert(collection_name=collection_name, data=embeddings_content)
    logger.info(f"知识库写入完成：insert_count={result.get('insert_count', 0)}")
    return state
