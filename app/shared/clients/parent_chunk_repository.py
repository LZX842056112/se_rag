"""章节级父块仓储（MongoDB）。

父块只按 ``parent_id`` 批量取回、**从不参与向量检索**，因此存 Mongo 而非 Milvus：

- 避开 Milvus ``VARCHAR(65535)`` 的字节上限，长章节可完整存储（Milvus 方案必须截断）；
- 无需为父块额外生成向量；
- 天然适配单测的 ``offline_guard``（``get_mongo_client`` 被 monkeypatch 抛错）。

写入按 ``doc_id`` 先删后插，与 Milvus 侧「按 file_title 先删后插」保持同一套幂等模式。
"""
from __future__ import annotations

from typing import Any

from app.shared.clients.mongo import get_collection
from app.shared.config import settings
from app.shared.runtime.logger import logger, step_log

# 查询侧只需要这几个字段，显式投影以避免把整章正文之外的冗余数据带回
_FETCH_PROJECTION = {
    "_id": 0,
    "parent_id": 1,
    "title": 1,
    "heading_path": 1,
    "content": 1,
    "page": 1,
}


class ParentChunkRepository:
    """父块集合的读写入口。"""

    @property
    def _collection(self):
        return get_collection(settings.mongo.parent_chunks_collection)

    @step_log("replace_document_parents")
    def replace_document_parents(self, doc_id: str, parents: list[dict[str, Any]]) -> int:
        """覆盖某文档的全部父块，返回写入条数。"""
        if not doc_id:
            raise ValueError("doc_id 为空，无法写入父块")
        collection = self._collection
        collection.delete_many({"doc_id": doc_id})
        if not parents:
            return 0
        collection.insert_many([dict(parent) for parent in parents])
        return len(parents)

    @step_log("fetch_parents")
    def fetch_by_ids(self, parent_ids: list[str]) -> dict[str, dict[str, Any]]:
        """按 ``parent_id`` 批量取回；失败返回空字典，由调用方降级为「不注入章节背景」。"""
        ids = [pid for pid in dict.fromkeys(parent_ids) if pid]
        if not ids:
            return {}
        try:
            cursor = self._collection.find({"parent_id": {"$in": ids}}, _FETCH_PROJECTION)
            return {document["parent_id"]: document for document in cursor}
        except Exception as exc:  # noqa: BLE001 - 父块属上下文增强，不可阻断问答
            logger.warning(f"父块读取失败，本次不注入章节背景：{exc}")
            return {}


parent_chunk_repository = ParentChunkRepository()
