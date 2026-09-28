"""接口契约与「导入期无外部连接」回归测试。"""
from __future__ import annotations

import app.api.http.import_server as import_server
import app.api.http.query_server as query_server
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
        "/api/evolution/candidates/{candidate_id}/approve",
        "/approval",
        "/static/app.js",
        "/static/app.css",
    ):
        assert expected in paths, f"缺少路由 {expected}"
    assert "/" in paths  # 客服对话页


def test_import_service_route_contract():
    paths = _paths(import_server.app)
    for expected in ("/api/health", "/api/import/upload", "/api/import/status/{task_id}",
                     "/import", "/static/app.js"):
        assert expected in paths, f"缺少路由 {expected}"


def test_no_external_connection_created_on_import():
    """导入服务模块不得建立任何外部连接（历史问题：导入即连 Mongo）。"""
    assert mongo_module._client is None
    assert milvus_module._milvus_client is None
    assert minio_module._minio_client is None
