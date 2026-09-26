"""
自进化 Mongo 持久化层：单例连接 + 集合读写。
写入规则：fb_events / k_gaps / k_candidates / k_metrics / param_registry 的写者分离，
读链路只读缓存，不直接读这些 collections。
"""
from __future__ import annotations

import os
import time
from typing import Any

from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.collection import Collection
from pymongo.database import Database

from app.shared.runtime.logger import logger

load_dotenv()


class EvolutionMongoTool:
    """基于原生 PyMongo 的自进化数据读写工具，仿 HistoryMongoTool 单例模式。"""

    def __init__(self) -> None:
        self.mongo_url: str = os.getenv("MONGO_URL", "")
        self.db_name: str = os.getenv("MONGO_DB_NAME", "")
        if not self.mongo_url:
            raise ValueError("MONGO_URL 未配置，无法建立自进化持久层连接")
        self.client: MongoClient = MongoClient(self.mongo_url)
        self.db: Database = self.client[self.db_name]

        # collections（懒创建，pyMongo 无需显式建表）
        self.fb_events: Collection = self.db["fb_events"]
        self.k_gaps: Collection = self.db["k_gaps"]
        self.k_candidates: Collection = self.db["k_candidates"]
        self.k_metrics: Collection = self.db["k_metrics"]
        self.param_registry: Collection = self.db["param_registry"]

        # 索引（create_index 幂等）
        self.fb_events.create_index([("session_id", 1), ("ts", -1)])
        self.fb_events.create_index([("ts", -1)])
        self.k_gaps.create_index([("session_id", 1), ("ts", -1)])
        self.k_candidates.create_index([("faq_question", 1)])
        self.k_candidates.create_index([("status", 1)])
        self.k_metrics.create_index([("ts", -1)])
        self.param_registry.create_index([("key", 1)], unique=True)

        logger.info(f"Successfully connected to MongoDB (evolution): {self.db_name}")

    # ---------------- param_registry ----------------
    def get_param(self, key: str) -> dict[str, Any] | None:
        return self.param_registry.find_one({"key": key})

    def set_param(self, key: str, value: Any, updated_by: str = "manual") -> None:
        """单写入者调用：upsert 且 rev+1。"""
        now = time.time()
        existing = self.param_registry.find_one({"key": key})
        rev = (existing or {}).get("rev", 0) + 1
        self.param_registry.update_one(
            {"key": key},
            {"$set": {"value": value, "updated_at": now, "updated_by": updated_by, "rev": rev}},
            upsert=True,
        )

    def all_params(self) -> dict[str, Any]:
        return {doc["key"]: doc.get("value") for doc in self.param_registry.find()}


# 单例
_evolution_mongo_tool: EvolutionMongoTool | None = None


def get_evolution_mongo_tool() -> EvolutionMongoTool:
    global _evolution_mongo_tool
    if _evolution_mongo_tool is None:
        _evolution_mongo_tool = EvolutionMongoTool()
    return _evolution_mongo_tool


evolution_repo = get_evolution_mongo_tool