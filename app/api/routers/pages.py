"""页面与静态资源路由（查询服务：``/`` 与 ``/approval``；导入服务：``/import``）。"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse

from app.shared.utils.paths import PROJECT_ROOT

_RESOURCES = PROJECT_ROOT / "app" / "resources"

# 两个服务共用的静态资源（前端三页共用同一套 CSS/JS）
static_router = APIRouter(tags=["pages"])
# 查询服务页面
query_pages_router = APIRouter(tags=["pages"])
# 导入服务页面
import_pages_router = APIRouter(tags=["pages"])


def _file_response(path: Path, media_type: str) -> FileResponse:
    """返回文件响应；路径固定为仓库内资源，不存在时由 FastAPI 抛出 500。"""
    return FileResponse(path=str(path), media_type=media_type)


@static_router.get("/static/app.js", include_in_schema=False)
def app_js() -> FileResponse:
    """三页共用的前端工具库。"""
    return _file_response(_RESOURCES / "js" / "app.js", "text/javascript")


@static_router.get("/static/app.css", include_in_schema=False)
def app_css() -> FileResponse:
    """三页共用的基础样式。"""
    return _file_response(_RESOURCES / "css" / "app.css", "text/css")


@query_pages_router.get("/", include_in_schema=False)
def chat_page() -> FileResponse:
    """客服对话页。"""
    return _file_response(_RESOURCES / "html" / "chat.html", "text/html")


@query_pages_router.get("/approval", include_in_schema=False)
def approval_page() -> FileResponse:
    """自进化候选审批后台。"""
    return _file_response(_RESOURCES / "html" / "approval.html", "text/html")


@import_pages_router.get("/import", include_in_schema=False)
def import_page() -> FileResponse:
    """知识库文件导入页。"""
    return _file_response(_RESOURCES / "html" / "import.html", "text/html")
