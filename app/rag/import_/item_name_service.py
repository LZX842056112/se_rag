"""主体识别服务：LLM 识别文档主体名 → 归并到库内标准名 → 写主体名索引并回填 chunks。"""
from __future__ import annotations

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.output_parsers import StrOutputParser
from pymilvus import DataType

from app.process.import_.agent.state import ImportGraphState
from app.rag.import_.config import CHUNKS_SPLIT_TOP_NUMBER
from app.rag.import_.inputs import load_chunks, load_subject
from app.rag.item_name.catalog import normalize_item_name
from app.rag.item_name.match import resolve_existing_item_name
from app.shared.clients.milvus_gateway import (
    eq_expr,
    milvus_gateway,
    register_vector_fields_and_indexes,
)
from app.shared.models import llm_providers
from app.shared.runtime.logger import logger, step_log
from app.shared.runtime.prompts import load_prompt


@step_log("recognize_item_name_by_chunks")
def recognize_item_name_by_chunks(chunks: list[dict], file_title: str) -> str:
    """用 LLM 从前 N 个切片中识别文档主体名；识别为空时回退文件标题。"""
    chain_client = llm_providers.chat()
    system_prompt_text = load_prompt("product_recognition_system")
    used_chunks = chunks[:CHUNKS_SPLIT_TOP_NUMBER]
    user_context = "".join(
        f"第{index}部分: 标题为:{chunk.get('title')} , 内容为: {chunk.get('content')} \n"
        for index, chunk in enumerate(used_chunks, start=1)
    )
    user_prompt_text = load_prompt("item_name_recognition", file_title=file_title, context=user_context)
    chain = chain_client | StrOutputParser()
    item_name = chain.invoke([
        SystemMessage(content=system_prompt_text),
        HumanMessage(content=user_prompt_text),
    ])
    if not item_name:
        logger.warning(f"模型未识别到主体名，使用 file_title 兜底：{file_title}")
        item_name = file_title
    return item_name.strip()


@step_log("resolve_item_name_against_catalog")
def resolve_item_name_against_catalog(item_name: str) -> str:
    """写入端主体归并：库内已有同一实体时复用其标准名，避免多次导入产生近重复主体名。

    与读取端共用同一套阈值/间距口径（见 ``app/rag/item_name/match.py``），因此
    「导入时归并到什么名字」与「提问时能确认到什么名字」天然一致。
    """
    resolved, evidence = resolve_existing_item_name(item_name)
    if evidence and normalize_item_name(resolved) != normalize_item_name(item_name):
        logger.warning(
            f"主体名归并：新识别[{item_name}] 命中库内同一实体[{resolved}]，"
            f"依据={evidence.get('matched_by')}，分={evidence.get('score')}，复用库内标准名"
        )
    return resolved


@step_log("prepare_item_name_collection")
def prepare_item_name_collection() -> None:
    """集合不存在时创建 ``kb_item_names``（schema + 索引）。"""
    client = milvus_gateway.milvus_client
    collection_name = milvus_gateway.item_name_collection_name
    if client.has_collection(collection_name=collection_name):
        return

    schema = client.create_schema(auto_id=True, enable_dynamic_field=True)
    schema.add_field(field_name="pk", datatype=DataType.INT64, is_primary=True)
    schema.add_field(field_name="file_title", datatype=DataType.VARCHAR, max_length=512)
    schema.add_field(field_name="item_name", datatype=DataType.VARCHAR, max_length=512)

    index_params = client.prepare_index_params()
    register_vector_fields_and_indexes(schema, index_params, dense_metric="COSINE")
    client.create_collection(collection_name=collection_name, schema=schema, index_params=index_params)
    logger.info(f"已创建主体名集合：{collection_name}")


@step_log("upsert_item_name")
def upsert_item_name(item_name: str, file_title: str) -> None:
    """按 ``file_title`` 幂等写入主体名（先删旧记录再插入）。"""
    result = llm_providers.generate_embeddings([item_name])
    client = milvus_gateway.milvus_client
    collection_name = milvus_gateway.item_name_collection_name
    client.delete(collection_name=collection_name, filter=eq_expr("file_title", file_title))
    client.insert(collection_name=collection_name, data=[{
        "file_title": file_title,
        "item_name": item_name,
        "dense_vector": result["dense"][0],
        "sparse_vector": result["sparse"][0],
    }])
    logger.info(f"主体名索引已更新：{item_name}（file_title={file_title}）")


@step_log("recognize_and_index_item_name")
def recognize_and_index_item_name(state: ImportGraphState) -> ImportGraphState:
    """主体识别服务入口：识别 → 归并 → 回填 chunks → 写主体名索引。"""
    chunks = load_chunks(state)
    file_title = load_subject(state, "file_title", default_name="default_title")

    item_name = recognize_item_name_by_chunks(chunks, file_title)
    prepare_item_name_collection()  # 归并检索要求集合已存在
    item_name = resolve_item_name_against_catalog(item_name)

    for chunk in chunks:
        chunk["item_name"] = item_name
    upsert_item_name(item_name, file_title)

    state["chunks"] = chunks
    state["item_name"] = item_name
    return state
