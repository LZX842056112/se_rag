"""接口契约与「导入期无外部连接」回归测试。"""
from __future__ import annotations

import os

import app.api.http.import_server as import_server
import app.api.http.query_server as query_server
from app.api.errors import ApiError
from app.api.routers import pages
from app.shared.clients import milvus_gateway as milvus_module
from app.shared.clients import minio_gateway as minio_module
from app.shared.clients import mongo as mongo_module


def _paths(app) -> set[str]:
    return {route.path for route in app.routes}


def test_query_service_route_contract():
    paths = _paths(query_server.app)
    for expected in (
        "/api/health",
        "/api/query",
        "/api/stream/{session_id}",
        "/api/history/{session_id}",
        "/api/evolution/feedback",
        "/api/evolution/candidates",
        "/api/evolution/status",
        "/api/evolution/candidates/{candidate_id}/approve",
        "/api/evolution/candidates/{candidate_id}",
        "/approval",
        "/static/{asset_name}",
    ):
        assert expected in paths, f"缺少路由 {expected}"
    assert "/" in paths  # 客服对话页


def test_import_service_route_contract():
    paths = _paths(import_server.app)
    for expected in ("/api/health", "/api/import/upload", "/api/import/status/{task_id}",
                     "/import", "/static/{asset_name}"):
        assert expected in paths, f"缺少路由 {expected}"


def test_no_external_connection_created_on_import():
    """导入服务模块不得建立任何外部连接（历史问题：导入即连 Mongo）。"""
    assert mongo_module._client is None
    assert milvus_module._milvus_client is None
    assert minio_module._minio_client is None


def test_static_asset_whitelist_and_cache_headers():
    """静态资源白名单生效，且带长缓存头；未登记的资源一律 404。"""
    for name in ("app.css", "app.js", "chat.css", "chat.js", "approval.js", "import.js"):
        response = pages.static_asset(name)
        assert response.headers["cache-control"] == "public, max-age=86400, immutable"
    try:
        pages.static_asset("secret.txt")
    except ApiError as exc:
        assert exc.status_code == 404
    else:
        raise AssertionError("未登记的资源未被拒绝（存在路径穿越风险）")


def test_pages_are_rendered_with_asset_version_and_no_cache():
    """页面渲染注入内容指纹，并禁止缓存 HTML（避免刷新后仍看到旧页面）。"""
    for name in ("chat", "approval", "import"):
        response = pages.render_page(name)
        body = response.body.decode("utf-8")
        assert response.headers["cache-control"] == "no-cache"
        assert "{{ASSET_V}}" not in body, "页面模板占位符未被替换"
        assert "?v=" in body, "静态资源缺少版本参数"
    assert len(pages.asset_version()) == 10


def test_health_reports_asset_version_for_stale_page_detection():
    """健康检查要带资源版本：前端据此发现「页面里的脚本已过期」并提示刷新。"""
    from app.api.routers import health as health_router

    payload = health_router.health()
    assert payload.code == 200
    assert payload.asset_version == pages.asset_version()


def test_asset_version_changes_with_content(tmp_path, monkeypatch):
    """内容指纹必须随静态资源内容变化（否则长缓存会锁死旧版本）。"""
    for relative, _ in pages._ASSETS.values():
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("v1", encoding="utf-8")
    monkeypatch.setattr(pages, "_RESOURCES", tmp_path)
    monkeypatch.setitem(pages._version_cache, "stamp", -1.0)
    first = pages.asset_version()
    # 指纹按「最新 mtime」做缓存失效判断：同一次 fs 时间粒度内的多次写入可能拿到相同 mtime
    # （Windows 上实测会偶发），因此显式把被改文件的 mtime 推后，保证测的是内容变化而非时钟精度
    changed = tmp_path / "js" / "app.js"
    changed.write_text("v2", encoding="utf-8")
    stamp = changed.stat().st_mtime + 5
    os.utime(changed, (stamp, stamp))
    assert pages.asset_version() != first
