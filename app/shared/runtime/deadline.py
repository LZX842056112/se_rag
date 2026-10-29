"""请求截止时间上下文。无配置加载或业务层依赖。"""
from contextvars import ContextVar
import time

query_deadline: ContextVar[float | None] = ContextVar("agentic_query_deadline", default=None)


def remaining() -> float | None:
    deadline = query_deadline.get()
    if deadline is None:
        return None
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise TimeoutError("agentic query deadline exceeded")
    return seconds


def bounded_timeout(configured):
    available = remaining()
    return min(float(configured), available) if available is not None else configured
