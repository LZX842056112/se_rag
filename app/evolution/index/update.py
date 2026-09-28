"""自进化条目索引：建 ``kb_evolution_items`` 集合并按 ``evo_doc_id`` upsert / 下架。

主键为显式 VARCHAR（非 auto_id），因此可以幂等 upsert 与按主键删除。
"""
from __future__ import annotations

from pymilvus import DataType

from app.evolution.models import KnowledgeCandidate
from app.shared.clients.milvus_gateway import (
    eq_expr,
    milvus_gateway,
    register_vector_fields_and_indexes,
)
from app.shared.models import llm_providers
from app.shared.runtime.logger import logger
from app.shared.utils.text import join_subjects


def canonical_subject(item_names: list[str]) -> str:
    """把候选主体名对齐到目录规范名（写入端/读取端主体名口径必须一致）。"""
    from app.rag.item_name.catalog import match_catalog_name

    resolved: list[str] = []
    for raw in item_names or []:
        name = str(raw or "").strip()
        if not name:
            continue
        hit = match_catalog_name(name)
        resolved.append(hit[0] if hit else name)
    return join_subjects(resolved) or "default_item_name"


def create_evolution_collection() -> bool:
    """集合不存在时创建（schema + 索引），与知识库集合同构建库。"""
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
    # dense 用 IP：BGE-M3 稠密向量已 L2 归一化，IP 与 COSINE 排序等价
    register_vector_fields_and_indexes(schema, index_params, dense_metric="IP")
    client.create_collection(collection_name=name, schema=schema, index_params=index_params)
    logger.info(f"已创建自进化集合：{name}")
    return True


def upsert_item(evo_doc_id: str, item: KnowledgeCandidate) -> bool:
    """按显式主键幂等写入一条候选条目。"""
    try:
        create_evolution_collection()
        subject = canonical_subject(item.item_names)
        embedding = llm_providers.embed_text(f"主体:{subject},内容:{item.faq_answer}")
        row = {
            "evo_doc_id": evo_doc_id,
            "faq_question": item.faq_question,
            "faq_answer": item.faq_answer,
            "source_refs": ",".join(item.source_refs),
            "item_name": subject,
            "status": item.status,
            "file_title": "__evolution__",
            "dense_vector": embedding["dense"][0],
            "sparse_vector": embedding["sparse"][0],
        }
        milvus_gateway.milvus_client.upsert(
            collection_name=milvus_gateway.evolution_collection_name, data=[row]
        )
        logger.info(f"自进化条目写入成功：{evo_doc_id}")
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error(f"自进化条目写入失败：{exc}")
        return False


def deactivate(evo_doc_id: str) -> bool:
    """下架/回滚：按主键删除，等价于移出在线检索。"""
    try:
        client = milvus_gateway.milvus_client
        name = milvus_gateway.evolution_collection_name
        if not client.has_collection(collection_name=name):
            return True
        client.delete(collection_name=name, filter=eq_expr("evo_doc_id", evo_doc_id))
        logger.info(f"自进化条目已下架：{evo_doc_id}")
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error(f"自进化条目下架失败：{exc}")
        return False
