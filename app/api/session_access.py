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


# 核验当前请求对指定会话的访问权限：会话 ID 本身不构成访问凭据，
# 归属取决于请求中携带的浏览器 Cookie（owner_token）对应的 owner_hash 是否与会话记录的归属一致。
def authorize_session(request: Request | None, session_id: str, *, create: bool = False):
    # 未提供请求上下文，或归属校验特性被关闭时，直接放行（跳过归属管理）。
    if request is None or not settings.api.session_ownership_enabled:
        return
    # 从请求中取得由 install_session_cookie 中间件写入的归属令牌（owner_token）。
    token = getattr(request.state, "owner_token", "")
    if not token:
        raise ApiError("session_not_found", "会话不可访问，请新建会话", 404)
    # 对令牌做哈希，形成不可逆的 owner_hash，避免在存储中暴露 Cookie 原值。
    owner = hashlib.sha256(token.encode()).hexdigest()
    try:
        sessions = get_collection(settings.mongo.query_sessions_collection)
        doc = sessions.find_one({"_id": session_id})
        if doc is None:
            # 无归属的旧历史不能由首次访问者认领。
            # 若会话已存在历史消息，则视为有归属数据，禁止无归属请求访问。
            if get_collection(settings.mongo.chat_message_collection).find_one({"session_id": session_id}, {"_id": 1}):
                raise ApiError("session_not_found", "会话不可访问，请新建会话", 404)
            # 仅为新建会话场景（create=True）时，才允许写入当前请求作为会话归属。
            if not create:
                return
            sessions.update_one({"_id": session_id}, {"$setOnInsert": {"owner_hash": owner}}, upsert=True)
            doc = sessions.find_one({"_id": session_id})
        # 比对会话记录的归属与当前请求归属，不一致则判定为无权访问。
        if not doc or not hmac.compare_digest(doc.get("owner_hash", ""), owner):
            raise ApiError("session_not_found", "会话不可访问，请新建会话", 404)
    except ApiError:
        raise
    # 归属核验依赖存储，存储异常时返回 503 而非暴露具体错误。
    except Exception as exc:
        raise ApiError("session_store_unavailable", "暂时无法核验会话归属，请稍后重试", 503) from exc