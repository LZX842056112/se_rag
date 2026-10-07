"""Milvus 集合 schema 迁移（一次性、需显式调用）。

全仓没有任何迁移框架：所有 ``create_*_collection`` 都是「集合存在即跳过」，因此 schema 变更
无法自动生效，必须显式重建。本模块是唯一迁移入口，且**默认只告警不执行**，避免误删数据。

破坏性：``kb_chunks`` 重建会清空集合内全部数据，需要重新导入文档。
父块存 MongoDB（无 schema 约束），其索引由 ``app.shared.clients.mongo.ensure_indexes`` 负责，
不在此处迁移。
"""
from __future__ import annotations

from app.rag.import_.index_service import prepare_chunks_collection
from app.shared.clients.milvus_gateway import milvus_gateway
from app.shared.runtime.logger import logger, step_log


@step_log("migrate_chunk_schema")
def migrate_chunk_schema(*, force: bool = False) -> dict:
    """按当前 schema 重建 ``kb_chunks``。

    :param force: 为 True 时才真正删除旧集合；否则仅告警返回
    :return: ``{"collection", "existed", "dropped"}``
    """
    client = milvus_gateway.milvus_client
    collection_name = milvus_gateway.chunk_collection_name
    if client is None:
        raise RuntimeError("Milvus 客户端不可用，无法执行 schema 迁移")

    existed = client.has_collection(collection_name=collection_name)
    if existed and not force:
        logger.warning(
            f"集合 {collection_name} 已存在。如需按新 schema（确定性 chunk_id + 元数据字段）"
            f"重建，请显式传入 force=True —— 该操作会删除集合内全部数据"
        )
        return {"collection": collection_name, "existed": existed, "dropped": False}

    if existed:
        client.drop_collection(collection_name=collection_name)
        logger.warning(f"已删除旧集合 {collection_name}，其数据需重新导入文档恢复")

    prepare_chunks_collection()
    logger.info(f"集合 {collection_name} 已按当前 schema 重建")
    return {"collection": collection_name, "existed": existed, "dropped": existed}
