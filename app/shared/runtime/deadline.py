"""请求截止时间上下文。无配置加载或业务层依赖。"""
from contextvars import ContextVar
import time

# 请求的绝对截止时刻（monotonic 时钟），由外层在上下文变量中设置。
# 无截止要求时取默认值 None（表示「未设置」）。用 ContextVar 使并发请求互不干扰，
# 也不需要在各调用链层层透传参数。
query_deadline: ContextVar[float | None] = ContextVar("agentic_query_deadline", default=None)


def remaining() -> float | None:
    """当前请求的剩余时长（秒）；未设置截止时返回 None，已超时抛 TimeoutError。"""
    deadline = query_deadline.get()
    if deadline is None:
        return None  # 未设置请求级截止
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        # 截止已过：抛 TimeoutError 让上层尽早终止，而不是带着负时长继续跑
        raise TimeoutError("agentic query deadline exceeded")
    return seconds


def bounded_timeout(configured):
    """在「配置超时」内收敛到请求剩余时间，防止嵌套调用超出请求级总时限。

    返回 ``min(configured, available)``；未设置请求截止时直接用配置值。
    """
    available = remaining()
    return min(float(configured), available) if available is not None else configured