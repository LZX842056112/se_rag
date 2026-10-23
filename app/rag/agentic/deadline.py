"""贯穿查询图模型客户端的请求截止时间。"""
from contextvars import ContextVar
import time
from functools import wraps

query_deadline = ContextVar('agentic_query_deadline', default=None)


def remaining():
    deadline = query_deadline.get()
    if deadline is None:
        return None
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise TimeoutError('agentic query deadline exceeded')
    return seconds


def bounded_timeout(configured):
    available = remaining()
    return min(float(configured), available) if available is not None else configured


def bounded_query(function):
    """将主题发现、历史与 Mongo 工作一并纳入请求预算。"""
    @wraps(function)
    def invoke(*args, **kwargs):
        from app.shared.config import settings
        from app.rag.agentic.budget import selected_mode
        import pymongo
        session = kwargs.get('session_id', args[0] if args else '')
        if selected_mode(session, mode=settings.agentic.mode,
                         percent=settings.agentic.gray_percent) != 'agentic':
            return function(*args, **kwargs)
        token = query_deadline.set(time.monotonic() + settings.agentic.timeout_seconds)
        try:
            with pymongo.timeout(settings.agentic.timeout_seconds):
                return function(*args, **kwargs)
        finally:
            query_deadline.reset(token)
    return invoke