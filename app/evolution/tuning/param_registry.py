"""
param_registry：读侧缓存封装。进程内 dict + 按需拉取，未命中回退到 query/config 常量。
写操作仅由 tuning/controller 或手工调用 set_param（单写入者）。
"""
from __future__ import annotations

import threading
import time
from typing import Any

from app.evolution.config import evolution_config
from app.evolution.repositories import get_evolution_mongo_tool
from app.rag.query.config import NODE_RRF_LIMIT_TOP, NODE_RRF_K, RERANK_MAX_TOPK

# 进程内缓存：{key: (value, rev)}，避免每次读取打 Mongo
_CACHE: dict[str, tuple[Any, int]] = {}
_CACHE_TTL = 10.0
_LAST_LOAD = 0.0
_LOCK = threading.Lock()

_FALLBACKS: dict[str, Any] = {
    "RRF_K": NODE_RRF_K,
    "RRF_TOP": NODE_RRF_LIMIT_TOP,
    "RERANK_TOP_K": RERANK_MAX_TOPK,
}


def _reload() -> None:
    global _LAST_LOAD
    now = time.time()
    if now - _LAST_LOAD < _CACHE_TTL or not getattr(evolution_config, "enabled", False):
        return
    try:
        records = get_evolution_mongo_tool().param_registry.find()
        for rec in records:
            _CACHE[rec["key"]] = (rec.get("value"), rec.get("rev", 0))
        _LAST_LOAD = now
    except Exception:
        pass


def get_param(key: str):
    with _LOCK:
        _reload()
        if key in _CACHE:
            return _CACHE[key][0]
    # 未命中回退常量
    if key in _FALLBACKS:
        return _FALLBACKS[key]
    return None


def get_all_params() -> dict[str, Any]:
    with _LOCK:
        _reload()
    return {key: _CACHE[key][0] for key in list(_CACHE)} | dict(_FALLBACKS)


def set_param(key: str, value: Any, updated_by: str = "metric") -> None:
    """写入口：由 controller（metric）或手工（manual）调用。"""
    get_evolution_mongo_tool().set_param(key, value, updated_by=updated_by)
    with _LOCK:
        _CACHE[key] = (value, get_evolution_mongo_tool().get_param(key).get("rev", 0))