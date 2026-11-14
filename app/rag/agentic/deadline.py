"""查询预算与 Mongo 超时边界。"""
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
        use_agentic = selected_mode(
            session, mode=settings.agentic.mode, percent=settings.agentic.gray_percent,
        ) == 'agentic'
        timeout = settings.agentic.timeout_seconds if use_agentic else settings.runtime.legacy_query_timeout_seconds
        deadline = time.monotonic() + timeout
        inherited = query_deadline.get()
        if not use_agentic and inherited is not None:
            deadline = min(deadline, inherited)
        token = query_deadline.set(deadline)
        try:
            if use_agentic:
                with pymongo.timeout(timeout):
                    return function(*args, **kwargs)
            # Legacy 使用 Mongo 客户端的选主时限。PyMongo 的请求时限会覆盖它。
            return function(*args, **kwargs)
        finally:
            query_deadline.reset(token)
    return invoke
