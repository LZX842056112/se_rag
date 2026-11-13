"""Redis 连接点与运行期状态后端选择（多 worker）。

设计约定：

1. **懒加载**：只 import 本模块不会建连接，避免离线单测与「只导入服务」时联网；
2. **单一开关**：``RUNTIME_STATE_BACKEND`` 决定用 Redis 还是进程内实现，
   ``auto`` 时由 ``REDIS_URL`` 是否存在决定；
3. **不做静默降级**：显式要求 Redis（``RUNTIME_STATE_BACKEND=redis``）但未配置时
   直接抛错；``auto`` 且未配置时走内存，保持现有单进程行为。
"""
from __future__ import annotations

import threading
from typing import Optional

from redis import Redis

from app.shared.config import settings
from app.shared.runtime.logger import logger

_client: Optional[Redis] = None
_lock = threading.Lock()

BACKEND_MEMORY = "memory"
BACKEND_REDIS = "redis"


def configured() -> bool:
    """是否配置了 Redis 连接串。"""
    return bool(str(settings.redis.url or "").strip())


def backend_name() -> str:
    """当前运行期状态后端：``redis`` 或 ``memory``。"""
    mode = str(settings.runtime.state_backend or "auto").strip().lower()
    if mode == BACKEND_REDIS:
        return BACKEND_REDIS
    if mode == BACKEND_MEMORY:
        return BACKEND_MEMORY
    return BACKEND_REDIS if configured() else BACKEND_MEMORY


def using_redis() -> bool:
    """是否使用 Redis 后端（调用处可读性更好）。"""
    return backend_name() == BACKEND_REDIS


def get_redis_client() -> Redis:
    """获取全局单例 Redis 客户端（首次调用才建连接）。"""
    global _client
    with _lock:
        if _client is None:
            if not configured():
                raise RuntimeError("REDIS_URL 未配置，无法建立 Redis 连接")
            _client = Redis.from_url(
                settings.redis.url,
                decode_responses=True,
                socket_timeout=settings.redis.timeout_seconds,
                socket_connect_timeout=settings.redis.timeout_seconds,
                health_check_interval=30,
            )
            logger.info("Redis 客户端已初始化")
        return _client


def ping() -> None:
    """启动自检：连接失败时抛错，由调用方决定是否阻断启动。"""
    get_redis_client().ping()


def set_client_for_tests(client: Optional[Redis]) -> None:
    """测试注入 fakeredis；传 ``None`` 恢复懒加载。"""
    global _client
    with _lock:
        _client = client


def close_redis_client() -> None:
    """关闭连接（脚本/测试收尾用）。"""
    global _client
    with _lock:
        client, _client = _client, None
    if client is not None:
        try:
            client.close()
        except Exception:  # noqa: BLE001 - 关闭失败不影响退出
            pass
        logger.info("Redis 客户端已关闭")
