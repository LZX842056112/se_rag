"""对话历史仓储：对上层屏蔽 MongoDB 细节（集合名与连接来自 ``settings``/``mongo``）。"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from bson import ObjectId

from app.shared.clients.mongo import get_collection
from app.shared.config import settings
from app.shared.runtime.logger import logger


class HistoryRepository:
    """会话历史读写。所有方法失败时降级（返回空/0）并记录日志，不抛给主链路。"""

    def _collection(self):
        return get_collection(settings.mongo.chat_message_collection)

    def list_recent(self, session_id: str, limit: int = 10) -> list[dict[str, Any]]:
        """按时间倒序取最近 ``limit`` 条消息（新 → 旧）。"""
        try:
            cursor = self._collection().find({"session_id": session_id}).sort("ts", -1).limit(limit)
            return list(cursor)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"读取会话历史失败：{exc}")
            return []

    def save_message(
        self,
        *,
        session_id: str,
        role: str,
        text: str,
        rewritten_query: str = "",
        item_names: Optional[list[str]] = None,
        image_urls: Optional[list[str]] = None,
        citations: Optional[list[dict]] = None,
        groundedness: float = 0.0,
        message_id: Optional[str] = None,
    ) -> str:
        """新增（无 ``message_id``）或更新（有 ``message_id``）一条消息。"""
        document = {
            "session_id": session_id,
            "role": role,
            "text": text,
            "rewritten_query": rewritten_query or "",
            "item_names": item_names,
            "image_urls": image_urls,
            "citations": citations or [],
            "groundedness": groundedness or 0.0,
            "ts": datetime.now().timestamp(),
        }
        collection = self._collection()
        if message_id:
            collection.update_one({"_id": ObjectId(message_id)}, {"$set": document})
            return message_id
        return str(collection.insert_one(document).inserted_id)

    def update_item_names(self, ids: list[str], item_names: list[str]) -> int:
        """批量更新历史消息的关联主体名。"""
        try:
            object_ids = [ObjectId(i) for i in ids]
            result = self._collection().update_many(
                {"_id": {"$in": object_ids}},
                {"$set": {"item_names": item_names}},
            )
            return result.modified_count
        except Exception as exc:  # noqa: BLE001
            logger.error(f"更新历史主体名失败：{exc}")
            return 0

    def clear_session(self, session_id: str) -> int:
        """清空指定会话的全部消息，返回删除条数。"""
        try:
            result = self._collection().delete_many({"session_id": session_id})
            logger.info(f"已清空会话 {session_id} 的 {result.deleted_count} 条记录")
            return result.deleted_count
        except Exception as exc:  # noqa: BLE001
            logger.error(f"清空会话历史失败：{exc}")
            return 0


history_repository = HistoryRepository()
