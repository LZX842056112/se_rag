"""请求截止时间上下文。无配置加载或业务层依赖。"""
from contextlib import contextmanager
from contextvars import ContextVar
import time

# 请求的绝对截止时刻（monotonic 时钟），由外层在上下文变量中设置。
# 无截止要求时取默认值 None（表示「未设置」）。用 ContextVar 使并发请求互不干扰，
# 也不需要在各调用链层层透传参数。
query_deadline: ContextVar[float | None] = ContextVar("query_deadline", default=None)


def remaining() -> float | None:
    """当前请求的剩余时长（秒）；未设置截止时返回 None，已超时抛 TimeoutError。"""
    deadline = query_deadline.get()
    if deadline is None:
        return None  # 未设置请求级截止
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        # 截止已过：抛 TimeoutError 让上层尽早终止，而不是带着负时长继续跑
        raise TimeoutError("query deadline exceeded")
    return seconds


def bounded_timeout(configured: float, *, reserve_seconds: float = 0.0) -> float:
    """取配置时限和可用请求时长中的较小值。"""
    available = remaining()
    if available is None:
        return configured
    available -= reserve_seconds
    if available <= 0:
        raise TimeoutError("query deadline reached reserved time")
    return min(float(configured), available)


@contextmanager
def deadline_window(configured: float, *, reserve_seconds: float = 0.0):
    """在当前请求内为一段工作设置更早的截止时间。"""
    started = time.monotonic()
    timeout = bounded_timeout(configured, reserve_seconds=reserve_seconds)
    token = query_deadline.set(started + timeout)
    try:
        yield
    finally:
        query_deadline.reset(token)
