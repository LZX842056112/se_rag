"""父块入库服务：把章节级父块写入 MongoDB。

父块是「命中子块后回填章节背景」的增强数据，不属于主检索产物：

- 写入失败**不阻断导入**——子块已写入 Milvus，缺少父块只会退化为「无章节背景」，而 Mongo
  并非本系统导入链路的既有依赖，不应因它不可用而让整次导入失败；
- 但失败以 error 级别记录，避免系统性故障被静默掩盖。
"""
from __future__ import annotations

from app.shared.clients.parent_chunk_repository import parent_chunk_repository
from app.shared.runtime.logger import logger, step_log


@step_log("index_parent_chunks")
def index_parent_chunks(state: dict) -> int:
    """写入某文档的全部父块，返回写入条数（失败返回 0）。"""
    parents = state.get("parent_chunks") or []
    if not parents:
        logger.warning("parent_chunks 为空，跳过父块写入；检索命中后将没有章节背景可回填")
        return 0

    doc_id = str(parents[0].get("doc_id") or "")
    try:
        written = parent_chunk_repository.replace_document_parents(doc_id, parents)
    except Exception as exc:  # noqa: BLE001 - 父块属增强数据，不可阻断导入
        logger.error(f"父块写入失败（doc_id={doc_id}），本次导入将缺少章节背景：{exc}")
        return 0

    logger.info(f"父块写入完成：doc_id={doc_id} count={written}")
    return written
