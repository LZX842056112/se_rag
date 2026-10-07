"""
MongoDB 唯一连接点与集合访问器（懒加载）。

历史问题：对话历史与自进化各建一套 ``MongoClient``（两套连接池），且历史模块在
**导入时**就联网，导致「只 import 服务模块」也会尝试连库。现统一为一份懒加载连接。

集合名全部来自 ``settings.mongo``（此前自进化仓储硬编码集合名，使配置项失效）。
"""
from __future__ import annotations

import threading
from typing import Optional

from pymongo import MongoClient
from pymongo.collection import Collection
from pymongo.database import Database

from app.shared.config import settings
from app.shared.config.common import env_int
from app.shared.runtime.logger import logger

_client: Optional[MongoClient] = None
_db: Optional[Database] = None
# 可重入锁：get_mongo_db() 会在持锁状态下调用 get_mongo_client()，
# 用普通 Lock 会自死锁（首次读取会话历史时永久卡住）。
_lock = threading.RLock()
_indexes_ready = False

# 选主/连接超时（毫秒）：Mongo 不可用时快速失败，避免热路径被默认 30s 卡住
_SERVER_SELECTION_TIMEOUT_MS = env_int("MONGO_SERVER_SELECTION_TIMEOUT_MS", 5000)


def get_mongo_client() -> MongoClient:
    """获取全局单例 MongoClient（首次调用才建立连接）。"""
    global _client
    with _lock:
        if _client is None:
            if not settings.mongo.url:
                raise ValueError("MONGO_URL 未配置，无法建立 MongoDB 连接")
            _client = MongoClient(
                settings.mongo.url,
                serverSelectionTimeoutMS=_SERVER_SELECTION_TIMEOUT_MS,
            )
            logger.info(f"MongoDB 客户端已初始化：db={settings.mongo.db_name}")
        return _client


def get_mongo_db() -> Database:
    """获取配置指定的数据库对象（懒加载）。"""
    global _db
    with _lock:
        if _db is None:
            _db = get_mongo_client()[settings.mongo.db_name]
        return _db


def get_collection(name: str) -> Collection:
    """按集合名获取集合对象。"""
    return get_mongo_db()[name]


def ensure_indexes() -> None:
    """创建各集合所需索引（幂等；失败只告警不影响主链路）。"""
    global _indexes_ready
    with _lock:
        if _indexes_ready:
            return
    try:
        db = get_mongo_db()
        mongo = settings.mongo
        db[mongo.chat_message_collection].create_index([("session_id", 1), ("ts", -1)])
        db[mongo.fb_events_collection].create_index([("session_id", 1), ("ts", -1)])
        db[mongo.fb_events_collection].create_index([("ts", -1)])
        db[mongo.k_gaps_collection].create_index([("session_id", 1), ("ts", -1)])
        db[mongo.k_candidates_collection].create_index([("faq_question", 1)])
        db[mongo.k_candidates_collection].create_index([("status", 1)])
        db[mongo.k_metrics_collection].create_index([("ts", -1)])
        db[mongo.param_registry_collection].create_index([("key", 1)], unique=True)
        db[mongo.parent_chunks_collection].create_index([("parent_id", 1)], unique=True)
        db[mongo.parent_chunks_collection].create_index([("doc_id", 1)])
    except Exception as exc:  # noqa: BLE001 - 索引属增强项，失败不阻断业务
        logger.warning(f"MongoDB 索引初始化失败：{exc}")
        return
    with _lock:
        _indexes_ready = True
    logger.info("MongoDB 索引已就绪")


def close_mongo_client() -> None:
    """关闭连接（脚本/测试收尾用）。"""
    global _client, _db
    with _lock:
        client, _client, _db = _client, None, None
    if client is not None:
        client.close()
        logger.info("MongoDB 客户端已关闭")
