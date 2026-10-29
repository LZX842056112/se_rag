"""Agentic 模式的查询预算与 Mongo 超时边界。"""
import time
from functools import wraps

from app.shared.runtime.deadline import query_deadline


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
