"""Validate mall identity and preserve the upstream commerce trust boundary."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import re
from typing import TypeVar

import httpx
from pydantic import BaseModel, TypeAdapter, ValidationError

from app.api.errors import ApiError
from app.commerce.models import (
    Action,
    ActionResult,
    CommerceId,
    Logistics,
    OrderDetail,
    OrderList,
    OrderStatus,
    ProductDetail,
    ProductList,
)
from app.shared.config import settings


_REQUEST_SECONDS = 5.0
_COOKIE_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")
_COOKIE_VALUE = re.compile(r"[\x21\x23-\x2B\x2D-\x3A\x3C-\x5B\x5D-\x7E]{1,4096}")
_ISSUED_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")
_UTC_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00)")
_VISIBLE_TOKEN = re.compile(r"[\x21-\x7E]+")
_USER_ID = TypeAdapter(CommerceId)
_READS = {
    "order_list": ("/users/{target}/orders", OrderList),
    "recent_products": ("/users/{target}/products", ProductList),
    "product_detail": ("/products/{target}", ProductDetail),
    "order_detail": ("/orders/{target}", OrderDetail),
    "order_status": ("/orders/{target}/status", OrderStatus),
    "logistics": ("/orders/{target}/logistics", Logistics),
}
_WRITES: dict[Action, str] = {
    "shipping_reminder": "shipping-reminders",
    "refund_application": "refund-applications",
}
_UPSTREAM_ERRORS = {
    (404, "commerce_not_found"): "找不到该商城记录",
    (404, "logistics_unavailable"): "该订单暂无物流信息",
    (400, "shipping_ineligible"): "当前订单状态无法催发货",
    (409, "refund_in_progress"): "该订单已有进行中的退款申请",
    (409, "idempotency_conflict"): "该确认请求与商城记录不一致",
    (422, "validation_error"): "商城无法接受此操作的参数",
}
_Data = TypeVar("_Data", bound=BaseModel)


class ActionRejected(ApiError):
    """The upstream explicitly rejected the action."""


def login_required() -> ApiError:
    return ApiError("commerce_login_required", "请先登录商城", 401)


def upstream_problem() -> ApiError:
    return ApiError("commerce_upstream_error", "商城服务返回异常，请稍后重试", 502)


def _unavailable() -> ApiError:
    return ApiError("commerce_unavailable", "商城服务暂不可用", 503)


def _http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=_REQUEST_SECONDS, trust_env=False, follow_redirects=False)


def _endpoint(url: str, token: str, *, base: bool = False) -> str:
    try:
        parsed = httpx.URL(url)
        if (
            parsed.scheme in {"http", "https"}
            and parsed.host
            and not parsed.userinfo
            and not parsed.fragment
            and (not base or not parsed.query)
            and _VISIBLE_TOKEN.fullmatch(token)
        ):
            return url.rstrip("/") if base else url
    except httpx.InvalidURL:
        pass
    raise _unavailable()


async def _request(
    method: str, url: str, headers: dict[str, str], payload: dict[str, str] | None = None,
) -> httpx.Response:
    try:
        async with asyncio.timeout(_REQUEST_SECONDS):
            async with _http_client() as client:
                if method == "GET":
                    try:
                        response = await client.request(method, url, headers=headers)
                    except httpx.ConnectError:
                        pass
                    else:
                        if response.status_code not in {502, 503, 504}:
                            return response
                        client.cookies.clear()
                return await client.request(method, url, headers=headers, json=payload)
    except (TimeoutError, httpx.TimeoutException) as exc:
        raise ApiError("commerce_timeout", "商城服务响应超时，请稍后重试", 504) from exc
    except httpx.NetworkError as exc:
        raise _unavailable() from exc
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        raise upstream_problem() from exc


def _response_json(response: httpx.Response) -> dict:
    try:
        data = response.json()
    except (ValueError, UnicodeError) as exc:
        raise upstream_problem() from exc
    if not isinstance(data, dict):
        raise upstream_problem()
    return data


def _identity_request(cookie_value: str | None) -> tuple[str, dict[str, str]]:
    config = settings.commerce
    url = _endpoint(config.session_verify_url, config.session_verify_token)
    if not _COOKIE_NAME.fullmatch(config.session_cookie_name):
        raise _unavailable()
    headers = {"Authorization": f"Bearer {config.session_verify_token}"}
    if cookie_value and _COOKIE_VALUE.fullmatch(cookie_value):
        headers["Cookie"] = f"{config.session_cookie_name}={cookie_value}"
    return url, headers


def _parse_expiry(value: object) -> datetime:
    if not isinstance(value, str) or not _UTC_TIMESTAMP.fullmatch(value):
        raise ValueError("Expected a UTC timestamp")
    return datetime.fromisoformat(value)


async def verify_identity(cookie_value: str | None) -> str:
    if not cookie_value or not _COOKIE_VALUE.fullmatch(cookie_value):
        raise login_required()
    url, headers = _identity_request(cookie_value)
    response = await _request("POST", url, headers)
    if response.status_code == 401:
        raise login_required()
    if response.is_server_error:
        raise _unavailable()
    if response.status_code != 200:
        raise upstream_problem()
    claims = _response_json(response)
    try:
        user_id = _USER_ID.validate_python(claims.get("user_id"))
        expires_at = _parse_expiry(claims.get("expires_at"))
    except (ValidationError, ValueError) as exc:
        raise upstream_problem() from exc
    if expires_at <= datetime.now(UTC):
        raise login_required()
    return user_id


async def _account_command(
    command: str, cookie_value: str | None, body: dict[str, str] | None = None,
) -> httpx.Response:
    url, headers = _identity_request(cookie_value)
    target = httpx.URL(url)
    if (
        not target.path.endswith("/verify")
        or target.query
        or (target.scheme != "https" and target.host not in {"127.0.0.1", "localhost", "::1"})
    ):
        raise _unavailable()
    path = target.path.removesuffix("/verify") + "/" + command
    response = await _request("POST", str(target.copy_with(path=path)), headers, body)
    if response.is_server_error:
        raise _unavailable()
    return response


async def open_customer_session(user_id: str, password: str, previous: str | None) -> tuple[str, int]:
    response = await _account_command("login", previous, {"user_id": user_id, "password": password})
    if response.status_code == 401:
        raise ApiError("commerce_login_failed", "无法登录。请核对账号和密码，或稍后重试。", 401)
    if response.status_code != 200:
        raise upstream_problem()
    issued = _response_json(response)
    token = issued.get("token")
    try:
        max_age = int((_parse_expiry(issued.get("expires_at")) - datetime.now(UTC)).total_seconds())
    except ValueError as exc:
        raise upstream_problem() from exc
    if not isinstance(token, str) or not _ISSUED_TOKEN.fullmatch(token) or max_age <= 0:
        raise upstream_problem()
    return token, max_age


async def close_customer_session(cookie_value: str | None) -> None:
    if cookie_value and _COOKIE_VALUE.fullmatch(cookie_value):
        response = await _account_command("logout", cookie_value)
        if response.status_code != 204:
            raise upstream_problem()


async def _call(
    method: str, path: str, *, user_id: str | None, idempotency_key: str | None = None,
    payload: dict[str, str] | None = None,
) -> httpx.Response:
    config = settings.commerce
    url = _endpoint(config.api_base_url, config.service_token, base=True) + path
    headers = {"Authorization": f"Bearer {config.service_token}"}
    if user_id is not None:
        headers["X-Commerce-User-Id"] = user_id
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    return await _request(method, url, headers, payload)


def _response_data(response: httpx.Response, model: type[_Data], *, write: bool = False) -> _Data:
    if response.is_server_error:
        raise _unavailable()
    body = _response_json(response)
    code = body.get("code")
    if response.status_code != 200:
        if isinstance(code, str):
            failure = (response.status_code, code)
            if failure in _UPSTREAM_ERRORS and (not write or code != "logistics_unavailable"):
                error_class = ActionRejected if write else ApiError
                raise error_class(code, _UPSTREAM_ERRORS[failure], response.status_code)
        raise upstream_problem()
    if type(code) is not int or code != 0 or body.get("message") != "ok":
        raise upstream_problem()
    try:
        return model.model_validate(body.get("data"))
    except ValidationError as exc:
        raise upstream_problem() from exc


async def _read(operation: str, target: str, user_id: str | None, model: type[_Data]) -> _Data:
    path, _ = _READS[operation]
    response = await _call("GET", path.format(target=target), user_id=user_id)
    return _response_data(response, model)


async def order_list(user_id: str) -> OrderList:
    result = await _read("order_list", user_id, user_id, OrderList)
    if result.user_id != user_id:
        raise upstream_problem()
    return result


async def recent_products(user_id: str) -> ProductList:
    result = await _read("recent_products", user_id, user_id, ProductList)
    if result.user_id != user_id:
        raise upstream_problem()
    return result


async def product_detail(product_id: str) -> ProductDetail:
    result = await _read("product_detail", product_id, None, ProductDetail)
    if result.product_id != product_id:
        raise upstream_problem()
    return result


async def order_detail(order_id: str, user_id: str) -> OrderDetail:
    result = await _read("order_detail", order_id, user_id, OrderDetail)
    if result.order_id != order_id:
        raise upstream_problem()
    return result


async def order_read(operation: str, order_id: str, user_id: str) -> OrderStatus:
    if operation == "order_detail":
        return await order_detail(order_id, user_id)
    _, model = _READS[operation]
    result = await _read(operation, order_id, user_id, model)
    if result.order_id != order_id:
        raise upstream_problem()
    return result


async def submit_action(
    operation: Action, order_id: str, user_id: str, confirmation_id: str, payload: dict[str, str],
) -> ActionResult:
    response = await _call(
        "POST", f"/orders/{order_id}/{_WRITES[operation]}",
        user_id=user_id, idempotency_key=confirmation_id, payload=payload,
    )
    result = _response_data(response, ActionResult, write=True)
    if result.order_id != order_id or result.request_type != operation:
        raise upstream_problem()
    return result
