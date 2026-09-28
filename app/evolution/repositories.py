"""自进化 Mongo 持久化层：集合访问器 + 参数注册表读写。

写入规则：``fb_events`` / ``k_gaps`` / ``k_candidates`` / ``k_metrics`` / ``param_registry``
各自有唯一写入者；读链路只读缓存（``tuning/param_registry``），不直接读这些集合。

连接与集合名统一来自 ``app.shared.clients.mongo`` + ``settings.mongo``：
历史问题——此处曾硬编码集合名并自建第二套 MongoClient，导致配置项失效与连接池翻倍。
"""
from __future__ import annotations

import time
from typing import Any, Optional

from app.shared.clients.mongo import get_collection
from app.shared.config import settings


class EvolutionRepository:
    """自进化各集合的访问入口（懒加载，不在导入期建立连接）。"""

    @property
    def fb_events(self):
        """反馈事件集合（用户显式反馈 + 读链路未解决信号）。"""
        return get_collection(settings.mongo.fb_events_collection)

    @property
    def k_gaps(self):
        """知识缺口集合。"""
        return get_collection(settings.mongo.k_gaps_collection)

    @property
    def k_candidates(self):
        """候选知识点集合（draft/active/rejected/deprecated）。"""
        return get_collection(settings.mongo.k_candidates_collection)

    @property
    def k_metrics(self):
        """指标快照集合。"""
        return get_collection(settings.mongo.k_metrics_collection)

    @property
    def param_registry(self):
        """参数注册表集合。"""
        return get_collection(settings.mongo.param_registry_collection)

    def get_param(self, key: str) -> Optional[dict[str, Any]]:
        """读取单条参数记录。"""
        return self.param_registry.find_one({"key": key})

    def set_param(self, key: str, value: Any, updated_by: str = "manual") -> None:
        """写入单条参数记录（单写入者调用：upsert 且 rev+1）。"""
        existing = self.param_registry.find_one({"key": key}) or {}
        self.param_registry.update_one(
            {"key": key},
            {"$set": {
                "value": value,
                "updated_at": time.time(),
                "updated_by": updated_by,
                "rev": int(existing.get("rev", 0)) + 1,
            }},
            upsert=True,
        )

    def all_params(self) -> dict[str, Any]:
        """读取全部参数（``{key: value}``）。"""
        return {doc["key"]: doc.get("value") for doc in self.param_registry.find()}


evolution_repo = EvolutionRepository()
