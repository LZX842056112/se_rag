"""参数注册表读侧缓存：进程内 dict + TTL 拉取，未命中回退到查询链路的常量。"""
from __future__ import annotations

import threading
import time
from typing import Any

from app.evolution.repositories import evolution_repo
from app.rag.query.config import NODE_RRF_K, NODE_RRF_LIMIT_TOP, RERANK_MAX_TOPK
from app.shared.config import settings
from app.shared.runtime.logger import logger

# 进程内缓存：``{key: (value, rev)}``，避免每次读取都打 Mongo
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
    """按 TTL 从 ``param_registry`` 刷新缓存（自进化关闭时跳过）。"""
    global _LAST_LOAD
    now = time.time()
    if now - _LAST_LOAD < _CACHE_TTL or not settings.evolution.enabled:
        return
    try:
        for record in evolution_repo.param_registry.find():
            _CACHE[record["key"]] = (record.get("value"), record.get("rev", 0))
    except Exception as exc:  # noqa: BLE001 - 注册表不可用时继续使用回退常量
        # 失败同样计入 TTL：否则 Mongo 不可用期间每次读取都会重试，热路径被反复拖慢
        _LAST_LOAD = now
        logger.warning(f"参数注册表加载失败，本次使用回退常量：{exc}")
        return
    _LAST_LOAD = now


def get_param(key: str):
    """读取参数值；未命中注册表时回退到内置常量。"""
    with _LOCK:
        _reload()
        if key in _CACHE:
            return _CACHE[key][0]
    return _FALLBACKS.get(key)


def get_all_params() -> dict[str, Any]:
    """读取全部参数（注册表值 + 回退常量的并集）。"""
    with _LOCK:
        _reload()
        cached = {key: value[0] for key, value in list(_CACHE.items())}
    return cached | dict(_FALLBACKS)


def set_param(key: str, value: Any, updated_by: str = "metric") -> None:
    """写入口：由 controller（metric）或人工（manual）调用。"""
    evolution_repo.set_param(key, value, updated_by=updated_by)
    record = evolution_repo.get_param(key) or {}
    with _LOCK:
        _CACHE[key] = (value, record.get("rev", 0))
