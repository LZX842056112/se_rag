"""统一错误模型与 FastAPI 异常处理器。

约定：所有错误响应体统一为 ``{"code": "<机器码>", "message": "<中文提示>"}``，
HTTP 状态码语义保持不变（400/401/404/422/500/503）。前端只需读 ``message``。
"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.shared.runtime.logger import logger


class ApiError(Exception):
    """业务错误：携带机器码、中文提示与 HTTP 状态码。"""

    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def error_body(code: str, message: str) -> dict[str, str]:
    """构造统一错误响应体。"""
    return {"code": code, "message": message}


def _validation_message(exc: RequestValidationError) -> str:
    """把 FastAPI 校验错误压成一句中文提示。"""
    details = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error.get("loc", []) if part != "body")
        details.append(f"{location}: {error.get('msg', '参数不合法')}")
    return "参数校验失败（" + "；".join(details) + "）" if details else "参数校验失败"


def install_error_handlers(app: FastAPI) -> None:
    """注册统一异常处理器（业务错误 / HTTP 错误 / 校验错误 / 兜底 500）。"""

    @app.exception_handler(ApiError)
    async def _handle_api_error(_request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=error_body(exc.code, exc.message))

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_error(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        detail = exc.detail if isinstance(exc.detail, str) else "请求失败"
        return JSONResponse(status_code=exc.status_code, content=error_body(f"http_{exc.status_code}", detail))

    @app.exception_handler(RequestValidationError)
    async def _handle_validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(status_code=422, content=error_body("validation_error", _validation_message(exc)))

    @app.exception_handler(Exception)
    async def _handle_unexpected(_request: Request, exc: Exception) -> JSONResponse:
        logger.exception(f"未处理异常：{exc}")
        return JSONResponse(status_code=500, content=error_body("internal_error", "服务内部错误"))
