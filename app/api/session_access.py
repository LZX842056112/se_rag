"""浏览器归属 Cookie 与持久化会话绑定。会话 ID 不作为访问凭据。"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets

from fastapi import Request

from app.api.errors import ApiError
from app.shared.clients.mongo import get_collection
from app.shared.config import settings

_COOKIE = "kb_owner"


def install_session_cookie(app):
    @app.middleware("http")
    async def owner_cookie(request: Request, call_next):
        token = request.cookies.get(_COOKIE, "")
        fresh = re.fullmatch(r"[a-f0-9]{64}", token) is None
        if fresh:
            token = secrets.token_hex(32)
        request.state.owner_token = token
        response = await call_next(request)
        if fresh:
            response.set_cookie(_COOKIE, token, httponly=True, samesite="strict",
                                secure=request.url.scheme == "https" or settings.api.app_env == "prod",
                                max_age=7 * 86400, path="/")
        return response


def authorize_session(request: Request | None, session_id: str, *, create: bool = False):
    if request is None or not settings.api.session_ownership_enabled:
        return
    token = getattr(request.state, "owner_token", "")
    if not token:
        raise ApiError("session_not_found", "会话不可访问，请新建会话", 404)
    owner = hashlib.sha256(token.encode()).hexdigest()
    try:
        sessions = get_collection(settings.mongo.query_sessions_collection)
        doc = sessions.find_one({"_id": session_id})
        if doc is None:
            # 无归属的旧历史不能由首次访问者认领。
            if get_collection(settings.mongo.chat_message_collection).find_one({"session_id": session_id}, {"_id": 1}):
                raise ApiError("session_not_found", "会话不可访问，请新建会话", 404)
            if not create:
                return
            sessions.update_one({"_id": session_id}, {"$setOnInsert": {"owner_hash": owner}}, upsert=True)
            doc = sessions.find_one({"_id": session_id})
        if not doc or not hmac.compare_digest(doc.get("owner_hash", ""), owner):
            raise ApiError("session_not_found", "会话不可访问，请新建会话", 404)
    except ApiError:
        raise
    except Exception as exc:
        raise ApiError("session_store_unavailable", "暂时无法核验会话归属，请稍后重试", 503) from exc
