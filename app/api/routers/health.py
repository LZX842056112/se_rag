"""系统路由：两个服务共用的健康检查。"""
from __future__ import annotations

from fastapi import APIRouter

from app.api.routers.pages import asset_version
from app.api.schema.query_schema import HealthResponseSchema

router = APIRouter(prefix="/api", tags=["system"])


@router.get("/health", response_model=HealthResponseSchema)
def health() -> HealthResponseSchema:
    """健康检查：进程存活即返回 200；附带静态资源版本，供前端识别「页面已过期」。"""
    return HealthResponseSchema(code=200, message="ok", asset_version=asset_version())
