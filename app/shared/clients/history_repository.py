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
            cursor = self._collection().find({"session_id": session_id}).sort([("ts", -1), ("_id", -1)]).limit(limit)
            return list(cursor)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"读取会话历史失败：{exc}")
            return []

    def list_valid_recent(self, session_id: str, limit: int = 10) -> list[dict[str, Any]]:
        """先筛选知识历史，再取条数。闲聊不占用知识上下文预算。"""
        try:
            return list(self._collection().find({
                "session_id": session_id, "item_names.0": {"$exists": True},
                "intent": {"$ne": "chitchat"},
            }).sort([("ts", -1), ("_id", -1)]).limit(limit))
        except Exception as exc:
            logger.warning(f"读取知识历史失败：{exc}")
            return []

    def get_answer(self, session_id: str, answer_id: str) -> dict | None:
        """只读取指定会话中的助手回答。无效 ID 或故障按不可核验处理。"""
        try:
            return self._collection().find_one({
                "_id": ObjectId(answer_id), "session_id": session_id, "role": "assistant",
            })
        except Exception:
            return None

    def attach_issue(self, session_id: str, answer_id: str, issue_id: str, handoff_required: bool):
        try:
            self._collection().update_one(
                {"_id": ObjectId(answer_id), "session_id": session_id, "role": "assistant"},
                {"$set": {"issue_id": issue_id, "handoff_required": handoff_required}},
            )
        except Exception as exc:
            logger.warning(f"保存回答待办信息失败：{exc}")

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
        groundedness: float | None = None,
        pending_subject_ask: Optional[dict] = None,
        message_id: Optional[str] = None,
        intent: str = "",
        turn_id: str = "",
        query: str = "",
        business_tag: str = "",
    ) -> str:
        """新增（无 ``message_id``）或更新（有 ``message_id``）一条消息。

        与其它方法一致：Mongo 故障时降级为空串并记录日志，不抛给主链路。
        答案已生成并推给用户后，落库失败不应再把整笔查询打成 500；
        下一轮读不到该条属可接受损失（不影响当轮已交付的答案）。

        ``intent``：本轮意图标记（"rag" / "chitchat" / 空串）。闲聊链路两条消息
        均记 "chitchat"，供历史回显渲染「闲聊」徽标；旧文档缺键按空串处理。
        """
        try:
            document = {
                "session_id": session_id,
                "role": role,
                "text": text,
                "rewritten_query": rewritten_query or "",
                "item_names": item_names or [],
                "image_urls": image_urls or [],
                "citations": citations or [],
                "groundedness": groundedness,
                # 反问轮（助手给出可点选主体）记下「触发反问的原话 + 候选主体名」：
                # 用户下一轮手输纯主体名时服务端据此复原意图——前端挂起态刷新即丢。
                # 空字典表示本轮不是反问。
                "pending_subject_ask": pending_subject_ask or {},
                "intent": intent or "",
                "turn_id": turn_id,
                "query": query,
                "business_tag": business_tag,
                "ts": datetime.now().timestamp(),
            }
            collection = self._collection()
            if message_id:
                collection.update_one({"_id": ObjectId(message_id)}, {"$set": document})
                return message_id
            return str(collection.insert_one(document).inserted_id)
        except Exception as exc:  # noqa: BLE001 - 落库失败降级，不阻断已完成的作答
            logger.error(f"写入会话历史失败：{exc}")
            return ""


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
