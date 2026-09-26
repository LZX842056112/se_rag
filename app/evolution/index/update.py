"""
自进化条目索引：建 kb_evolution_items 集合 + 按 evo_doc_id upsert / 下架。
主键为显式 VARCHAR（非 auto_id），故可幂等 upsert 与 delete。
"""
from __future__ import annotations

from pymilvus import DataType

from app.evolution.models import KnowledgeCandidate
from app.infra.llm.providers import llm_providers
from app.infra.vector_store.milvus_gateway import milvus_gateway
from app.shared.clients import register_vector_fields_and_indexes
from app.shared.runtime.logger import logger


def _embedding_text(item: KnowledgeCandidate) -> str:
    subject = ",".join(item.item_names) or "default_item_name"
    return f"主体:{subject},内容:{item.faq_answer}"


def create_evolution_collection() -> bool:
    """集合不存在则创建（schema + 索引），与 kb_chunks 同构建库。返回是否存在/已就绪。"""
    client = milvus_gateway.milvus_client
    name = milvus_gateway.evolution_collection_name
    if client.has_collection(collection_name=name):
        return True
    schema = client.create_schema(auto_id=False, enable_dynamic_field=True)
    schema.add_field(field_name="evo_doc_id", datatype=DataType.VARCHAR, max_length=128, is_primary=True)
    schema.add_field(field_name="faq_question", datatype=DataType.VARCHAR, max_length=512)
    schema.add_field(field_name="faq_answer", datatype=DataType.VARCHAR, max_length=65535)
    schema.add_field(field_name="source_refs", datatype=DataType.VARCHAR, max_length=2048)
    schema.add_field(field_name="item_name", datatype=DataType.VARCHAR, max_length=512)
    schema.add_field(field_name="status", datatype=DataType.VARCHAR, max_length=32)
    schema.add_field(field_name="file_title", datatype=DataType.VARCHAR, max_length=512)

    index_params = client.prepare_index_params()
    # dense 用 IP：BGE-M3 稠密向量已 L2 归一化，IP==COSINE 排序等价（与 kb_chunks/milvus_utils 一致）
    register_vector_fields_and_indexes(schema, index_params, dense_metric="IP")
    client.create_collection(collection_name=name, schema=schema, index_params=index_params)
    logger.info(f"已创建自进化集合: {name}")
    return True


def upsert_item(evo_doc_id: str, item: KnowledgeCandidate) -> bool:
    """按显式主键 upsert 一条候选（幂等）。"""
    try:
        create_evolution_collection()
        emb = llm_providers.generate_embeddings([_embedding_text(item)])
        row = {
            "evo_doc_id": evo_doc_id,
            "faq_question": item.faq_question,
            "faq_answer": item.faq_answer,
            "source_refs": ",".join(item.source_refs),
            "item_name": ",".join(item.item_names) or "default_item_name",
            "status": item.status,
            "file_title": "__evolution__",
            "dense_vector": emb["dense"][0],
            "sparse_vector": emb["sparse"][0],
        }
        milvus_gateway.milvus_client.upsert(
            collection_name=milvus_gateway.evolution_collection_name, data=[row]
        )
        logger.info(f"自进化条目 upsert 成功: {evo_doc_id}")
        return True
    except Exception as e:
        logger.error(f"自进化条目 upsert 失败: {e}")
        return False


def deactivate(evo_doc_id: str) -> bool:
    """下架/回滚：按主键删除，等价于移出在线检索。"""
    try:
        client = milvus_gateway.milvus_client
        name = milvus_gateway.evolution_collection_name
        if not client.has_collection(collection_name=name):
            return True
        client.delete(collection_name=name, filter=f"evo_doc_id == '{evo_doc_id}'")
        logger.info(f"自进化条目下架: {evo_doc_id}")
        return True
    except Exception as e:
        logger.error(f"自进化条目下架失败: {e}")
        return False