"""自进化调度器的主节点租约（多 worker）。

背景：调度循环随查询服务启动。多 worker 下每个进程都会启动一份，
指标快照、参数自调与回测会被重复执行。缺口处理虽有持久化租约，
其余周期任务没有，因此需要一个跨进程的主节点选举。

实现：Redis ``SET NX EX`` 抢主，后台任务定期续约；抢不到或续约失败
就等待下一轮，保证同一时刻只有一个实例在跑调度。
"""
from __future__ import annotations

import asyncio
import uuid

from app.evolution.scheduler import evolution_scheduler_loop
from app.shared.clients.redis_gateway import get_redis_client
from app.shared.config import settings
from app.shared.runtime.logger import logger

LEADER_KEY = "evolution:scheduler:leader"

# 只有值仍属于本实例时才续期，避免误续其他实例的租约
_RENEW_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('EXPIRE', KEYS[1], ARGV[2])
end
return 0
"""


def try_acquire_leader(token: str) -> bool:
    """尝试抢占主节点；Redis 异常按未抢到处理。"""
    ttl = settings.runtime.scheduler_leader_ttl_seconds
    try:
        return bool(get_redis_client().set(LEADER_KEY, token, nx=True, ex=ttl))
    except Exception as exc:  # noqa: BLE001 - 抢主失败由下一轮重试
        logger.warning(f"调度器主节点抢占失败（稍后重试）：{exc}")
        return False


def renew_leader(token: str) -> bool:
    """续约；返回 False 表示本实例已不再是主节点。"""
    ttl = settings.runtime.scheduler_leader_ttl_seconds
    try:
        result = get_redis_client().eval(_RENEW_SCRIPT, 1, LEADER_KEY, token, ttl)
        return int(result or 0) == 1
    except Exception as exc:  # noqa: BLE001 - 续约失败按失去主节点处理
        logger.warning(f"调度器主节点续约失败：{exc}")
        return False


async def scheduler_leader_loop(interval_seconds: float) -> None:
    """主节点循环：抢到租约后运行调度，租约丢失则停止并重新竞争。"""
    token = uuid.uuid4().hex
    ttl = max(30, settings.runtime.scheduler_leader_ttl_seconds)
    while True:
        if not await asyncio.to_thread(try_acquire_leader, token):
            await asyncio.sleep(ttl / 3)
            continue

        logger.info(f"已成为自进化调度主节点：token={token[:8]}")
        loop_task = asyncio.create_task(evolution_scheduler_loop(interval_seconds))
        try:
            while True:
                await asyncio.sleep(ttl / 3)
                if not await asyncio.to_thread(renew_leader, token):
                    logger.warning("自进化调度主节点租约丢失，本实例停止调度")
                    break
        finally:
            loop_task.cancel()
            try:
                await loop_task
            except asyncio.CancelledError:
                pass
