"""页面与静态资源路由。

设计要点：
1. **静态资源白名单**：`/static/{asset}` 只允许下表列出的文件，天然免疫路径穿越；
2. **内容指纹**：静态资源在 HTML 中以 `?v=<内容哈希>` 引用，文件一改版本号即变，
   因此可以放心地给资源加长缓存（`max-age=86400, immutable`），
   而 HTML 本身用 `no-cache`（每次校验，避免刷新后仍看到旧页面）；
3. 页面渲染注入资源版本和 ASGI 挂载路径。
"""
from __future__ import annotations

import hashlib
from html import escape

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse

from app.api.errors import ApiError
from app.shared.config import settings
from app.shared.utils.paths import PROJECT_ROOT

_RESOURCES = PROJECT_ROOT / "app" / "resources"

# 静态资源白名单：URL 名 -> (相对路径, media_type)
_ASSETS: dict[str, tuple[str, str]] = {
    "app.css": ("css/app.css", "text/css"),
    "app.js": ("js/app.js", "text/javascript"),
    "chat.css": ("css/chat.css", "text/css"),
    "chat-commerce.js": ("js/chat-commerce.js", "text/javascript"),
    "chat.js": ("js/chat.js", "text/javascript"),
    "approval.css": ("css/approval.css", "text/css"),
    "approval.js": ("js/approval.js", "text/javascript"),
    "import.css": ("css/import.css", "text/css"),
    "import.js": ("js/import.js", "text/javascript"),
    "workflows.css": ("css/workflows.css", "text/css"),
    "workflow-scene-config.js": ("js/workflow-scene-config.js", "text/javascript"),
    "workflow-scene.js": ("js/workflow-scene.js", "text/javascript"),
    "workflows.js": ("js/workflows.js", "text/javascript"),
}

# 页面白名单：页面名 -> html 相对路径
_PAGES: dict[str, str] = {
    "chat": "html/chat.html",
    "approval": "html/approval.html",
    "import": "html/import.html",
    "workflows": "html/workflows.html",
}

# 资源版本号缓存：内容或 mtime 变化时重算（避免每次请求都读全部静态文件）
_version_cache: dict[str, object] = {"stamp": -1.0, "value": ""}


def asset_version() -> str:
    """返回静态资源内容指纹（10 位十六进制）。"""
    paths = sorted(_RESOURCES / rel for rel, _ in _ASSETS.values())
    stamp = max((path.stat().st_mtime for path in paths), default=0.0)
    if _version_cache["stamp"] != stamp:
        digest = hashlib.sha1()
        for path in paths:
            digest.update(path.read_bytes())
        _version_cache["stamp"] = stamp
        _version_cache["value"] = digest.hexdigest()[:10]
    return str(_version_cache["value"])


def render_page(name: str, request: Request | None = None) -> HTMLResponse:
    """读取页面模板并注入资源版本和挂载路径。"""
    relative = _PAGES.get(name)
    if relative is None:
        raise ApiError("page_not_found", "页面不存在", status_code=404)
    html = (_RESOURCES / relative).read_text(encoding="utf-8")
    root_path = request.scope.get("root_path", "") if request is not None else ""
    commerce_enabled = "true" if settings.commerce.enabled else "false"
    return HTMLResponse(
        html.replace("{{ASSET_V}}", asset_version())
        .replace("{{ROOT_PATH}}", escape(root_path.rstrip("/"), quote=True))
        .replace("{{COMMERCE_ENABLED}}", commerce_enabled),
        headers={"Cache-Control": "no-cache"},
    )


# 两个服务共用的静态资源
static_router = APIRouter(tags=["pages"])
# 查询服务页面
query_pages_router = APIRouter(tags=["pages"])
# 导入服务页面
import_pages_router = APIRouter(tags=["pages"])


@static_router.get("/static/{asset_name}", include_in_schema=False)
def static_asset(asset_name: str) -> FileResponse:
    """返回白名单内的静态资源（带内容指纹的长缓存）。"""
    entry = _ASSETS.get(asset_name)
    if entry is None:
        raise ApiError("asset_not_found", "静态资源不存在", status_code=404)
    relative, media_type = entry
    return FileResponse(
        path=str(_RESOURCES / relative),
        media_type=media_type,
        headers={"Cache-Control": "public, max-age=86400, immutable"},
    )


@query_pages_router.get("/", include_in_schema=False)
def chat_page(request: Request) -> HTMLResponse:
    """客服对话页。"""
    return render_page("chat", request)


@query_pages_router.get("/approval", include_in_schema=False)
def approval_page(request: Request) -> HTMLResponse:
    """自进化候选审批后台。"""
    return render_page("approval", request)


@query_pages_router.get("/workflows", include_in_schema=False)
def workflows_page(request: Request) -> HTMLResponse:
    return render_page("workflows", request)


@import_pages_router.get("/import", include_in_schema=False)
def import_page(request: Request) -> HTMLResponse:
    """知识库文件导入页。"""
    return render_page("import", request)
