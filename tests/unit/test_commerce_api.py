"""Exercise the commerce routes with fake HTTP services and shared Mongo records."""
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import json
from threading import Barrier, Event
from uuid import UUID

import httpx
import mongomock
import pytest
from fastapi.testclient import TestClient

from app.api.http.query_server import app
from app.shared.config import settings


ORIGIN = "https://help.example.test"
VERIFY = "https://identity.example.test/verify"
UPSTREAM = "https://mall-api.example.test"
SESSION = "/api/commerce/session"
ASSIST = "/api/commerce/assist"
CONFIRMATIONS = "/api/commerce/confirmations"
TASK = "/api/commerce/task"
OBJECTS = "/api/commerce/objects"
DRAFT_SESSION = "sess-commerce-task"
ORDER = "O1001"
PRODUCT = "P1001"
ORDER_DETAIL = {
    "order_id": ORDER, "status": "待发货", "status_desc": "等待发货",
    "amount": "99.10", "created_at": "2026-11-01T10:00:00",
    "receiver_name": "private-name", "receiver_phone_masked": "private-phone",
    "receiver_address": "private-address",
    "items": [{"product_id": PRODUCT, "title": "路由器", "quantity": 1, "price": "99.10",
               "contact": "private-contact"}],
}
ACTION_RESULT = {
    "request_type": "shipping_reminder", "request_id": "R1001",
    "order_id": ORDER, "status": "submitted", "status_desc": "提醒已提交",
    "private_payload": "private-extra",
}


def _success(data):
    return httpx.Response(200, json={"code": 0, "message": "ok", "data": data})


class FakeServices:
    def __init__(self):
        self.requests = []
        self.action = lambda _: _success(ACTION_RESULT)
        self.read_response = None
        self.verify_response = None
        self.expires_at = datetime.now(UTC) + timedelta(hours=1)

    def send(self, request):
        self.requests.append(request)
        if str(request.url) == VERIFY:
            if self.verify_response is not None:
                return self.verify_response
            subject = {"mall_session=alice-session": "alice",
                       "mall_session=bob-session": "bob"}.get(request.headers.get("cookie"))
            if subject is None:
                return httpx.Response(401)
            return httpx.Response(200, json={"user_id": subject, "expires_at": self.expires_at.isoformat(),
                                             "email": "private-identity"})
        if request.method == "POST":
            return self.action(request)
        if self.read_response is not None:
            return self.read_response
        if request.url.path == "/users/alice/orders":
            return _success({"user_id": "alice", "orders": [{
                key: ORDER_DETAIL[key] for key in ("order_id", "status", "amount", "created_at")
            } | {"title": "路由器", "private": "private-list"}]})
        if request.url.path == "/users/alice/products":
            return _success({"user_id": "alice", "products": [{
                "product_id": PRODUCT, "title": "路由器", "price": "99.10", "private": "private-list",
            }]})
        if request.url.path in {f"/orders/{ORDER}", f"/orders/{ORDER}/status"}:
            if request.headers.get("x-commerce-user-id") != "alice":
                return httpx.Response(404, json={"code": "commerce_not_found"})
            if request.url.path.endswith("/status"):
                return _success({key: ORDER_DETAIL[key] for key in ("order_id", "status", "status_desc")})
            return _success(ORDER_DETAIL)
        if request.url.path == f"/products/{PRODUCT}":
            return _success({
                "product_id": PRODUCT, "title": "路由器", "description": "双频",
                "price": "99.10", "stock_status": "in_stock", "cover_url": "javascript:alert(1)",
                "attributes": {"ports": 4, "color": "white", "nested": {"private": "secret"}},
                "private": "private-product-data",
            })
        return httpx.Response(404, json={"code": "commerce_not_found", "detail": "private-extra"})

    @property
    def actions(self):
        return [request for request in self.requests
                if request.method == "POST" and request.url.host == "mall-api.example.test"]


@pytest.fixture
def commerce(monkeypatch):
    from app.commerce import confirmations, gateway

    services = FakeServices()
    db = mongomock.MongoClient().test
    clock = [datetime.now(UTC).replace(microsecond=0)]
    monkeypatch.setattr(confirmations, "get_collection", lambda name: db[name])
    monkeypatch.setattr(confirmations, "_now", lambda: clock[0])
    monkeypatch.setattr("app.shared.clients.mongo.get_collection", lambda name: db[name])
    monkeypatch.setattr(gateway, "_http_client", lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(services.send),
    ))
    values = {
        "enabled": True, "actions_enabled": True, "api_base_url": UPSTREAM,
        "service_token": "internal-service-token", "session_verify_url": VERIFY,
        "session_verify_token": "internal-verify-token", "session_cookie_name": "mall_session",
        "public_origin": ORIGIN,
    }
    for key, value in values.items():
        monkeypatch.setattr(settings.commerce, key, value)
    client = TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN}, cookies={
        "mall_session": "alice-session", "other_session": "never-forward", "kb_owner": "a" * 64,
    })
    return services, client, db, clock


def test_disabled_commerce_reports_status_without_external_services(monkeypatch):
    monkeypatch.setattr(settings.commerce, "enabled", False)
    monkeypatch.setattr(settings.commerce, "actions_enabled", True)
    response = TestClient(app).get(SESSION)
    assert response.status_code == 200
    assert response.json() == {"enabled": False, "actions_enabled": False, "authenticated": False}
    assert response.headers["cache-control"] == "no-store"


def test_mall_login_and_logout_issue_only_a_scoped_credential_cookie(commerce, monkeypatch):
    services, _, _, _ = commerce
    monkeypatch.setattr(settings.commerce, "account_login_enabled", True, raising=False)
    site = TestClient(app, base_url=ORIGIN, root_path="/support", headers={"Origin": ORIGIN}, cookies={
        "kb_owner": "a" * 64, "unrelated": "never-forward",
    })
    login_path = "/support/api/commerce/login"
    logout_path = "/support/api/commerce/logout"
    token = "a" * 43
    password = "test-password  "

    def respond(request):
        assert request.headers["authorization"] == "Bearer internal-verify-token"
        assert request.url.host == "identity.example.test"
        if request.url.path == "/login":
            assert "cookie" not in request.headers
            assert json.loads(request.content) == {"user_id": "alice", "password": password}
            return httpx.Response(200, json={"token": token, "expires_at": services.expires_at.isoformat()})
        assert request.url.path == "/logout"
        assert request.headers["cookie"] == "mall_session=" + token
        return httpx.Response(204)

    services.action = respond
    assert site.get("/support" + SESSION).json()["account_login_enabled"] is True
    forbidden = site.post(login_path, json={"user_id": "alice", "password": password}, headers={
        "Origin": "https://other.example.test",
    })
    assert forbidden.status_code == 403
    assert not services.requests

    signed_in = site.post(login_path, json={"user_id": "alice", "password": password})
    assert signed_in.status_code == 200
    assert signed_in.json() == {"authenticated": True}
    assert token not in signed_in.text
    cookie = signed_in.headers["set-cookie"].lower()
    for attribute in ("httponly", "secure", "samesite=strict", "path=/support/"):
        assert attribute in cookie
    assert signed_in.headers["cache-control"] == "no-store"

    signed_out = site.post(logout_path, json={})
    assert signed_out.status_code == 200
    assert signed_out.json() == {"authenticated": False}
    assert "Max-Age=0" in signed_out.headers["set-cookie"]
    assert "mall_session" not in site.cookies


def test_mall_login_does_not_accept_a_malformed_or_expired_issued_token(commerce, monkeypatch):
    services, client, _, _ = commerce
    monkeypatch.setattr(settings.commerce, "account_login_enabled", True, raising=False)
    for token, expires in (
        ("malformed", services.expires_at),
        ("a" * 43, datetime.now(UTC) - timedelta(seconds=1)),
    ):
        services.action = lambda _: httpx.Response(200, json={"token": token, "expires_at": expires.isoformat()})
        response = client.post("/api/commerce/login", json={"user_id": "alice", "password": "test-password"})
        assert response.status_code == 502
        assert response.json()["code"] == "commerce_upstream_error"
        assert "set-cookie" not in response.headers


def test_private_calls_reverify_and_send_only_the_authenticated_subject(commerce):
    services, client, _, _ = commerce
    assert client.get(SESSION).json() == {
        "enabled": True, "actions_enabled": True, "authenticated": True,
    }
    response = client.post(ASSIST, json={"operation": "order_detail", "order_id": ORDER}, headers={
        "Authorization": "Bearer forged", "X-Commerce-User-Id": "bob",
    })
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "type": "result", "operation": "order_detail",
        "result": {
            "order_id": ORDER, "status": "待发货", "status_desc": "等待发货",
            "amount": "99.10", "created_at": "2026-11-01T10:00:00",
            "items": [{"product_id": PRODUCT, "title": "路由器", "quantity": 1, "price": "99.10"}],
        },
    }
    first, second, upstream = services.requests
    for verify in (first, second):
        assert verify.method == "POST"
        assert str(verify.url) == VERIFY
        assert verify.headers["cookie"] == "mall_session=alice-session"
        assert verify.headers["authorization"] == "Bearer internal-verify-token"
        assert "x-commerce-user-id" not in verify.headers
    assert upstream.headers["x-commerce-user-id"] == "alice"
    assert upstream.headers["authorization"] == "Bearer internal-service-token"
    assert "cookie" not in upstream.headers


def test_expired_verified_session_cannot_use_a_private_operation(commerce):
    services, client, _, _ = commerce
    services.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert client.get(SESSION).json()["authenticated"] is False
    result = client.post(ASSIST, json={"operation": "order_list"})
    assert result.status_code == 401
    assert result.json()["code"] == "commerce_login_required"
    assert all(str(request.url) == VERIFY for request in services.requests)


def test_verifier_authentication_failure_is_a_service_error(commerce):
    services, client, _, _ = commerce
    services.verify_response = httpx.Response(403, json={"message": "private-extra"})
    response = client.post(ASSIST, json={"operation": "order_list"})
    assert response.status_code == 502
    assert response.json()["code"] == "commerce_upstream_error"
    assert len(services.requests) == 1


def test_explicit_public_product_lookup_never_verifies_or_forwards_cookie(commerce):
    services, client, _, _ = commerce
    response = client.post(ASSIST, json={"operation": "product_detail", "product_id": PRODUCT})
    assert response.status_code == 200
    result = response.json()["result"]
    assert result["price"] == "99.10"
    assert result["attributes"] == {"ports": 4, "color": "white"}
    assert result.get("cover_url") is None
    assert "private" not in result
    (upstream,) = services.requests
    assert upstream.url.path == f"/products/{PRODUCT}"
    assert "x-commerce-user-id" not in upstream.headers
    assert "cookie" not in upstream.headers


def test_anonymous_product_selection_prompts_for_id_without_private_read(commerce):
    services, _, _, _ = commerce
    visitor = TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN})
    response = visitor.post(ASSIST, json={"query": "打印机有货吗"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["type"] == "choose"
    assert payload["operation"] == "product_detail"
    assert payload["choices"] == []
    assert "ID" in payload["message"]
    assert services.requests == []


@pytest.mark.parametrize("origin", ["https://evil.example.test", "null", ORIGIN + "/"])
def test_commerce_posts_require_exact_origin_before_contacting_services(commerce, origin):
    services, client, _, _ = commerce
    response = client.post(ASSIST, json={"operation": "order_list"}, headers={"Origin": origin})
    assert response.status_code == 403
    assert response.json()["code"] == "commerce_origin_forbidden"
    assert response.headers["cache-control"] == "no-store"
    assert services.requests == []


def test_origin_and_no_store_checks_cover_proxy_root_path(commerce):
    services, _, _, _ = commerce
    proxy = TestClient(app, base_url=ORIGIN + "/help", root_path="/help")
    response = proxy.post(ASSIST, json={"operation": "order_list"})
    assert response.status_code == 403
    assert response.json()["code"] == "commerce_origin_forbidden"
    assert response.headers["cache-control"] == "no-store"
    assert services.requests == []


def test_implicit_logistics_selects_from_recent_owned_orders(commerce):
    services, client, _, _ = commerce
    response = client.post(ASSIST, json={"query": "我的订单到哪了"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["type"] == "choose"
    assert payload["operation"] == "logistics"
    assert [choice["id"] for choice in payload["choices"]] == [ORDER]
    assert [request.url.path for request in services.requests] == ["/verify", "/users/alice/orders"]


def test_policy_choice_carries_clean_policy_question_and_order_target(commerce):
    services, client, _, _ = commerce
    policy = client.post(ASSIST, json={"query": "怎么申请退款"})
    assert policy.json() == {"type": "knowledge"}
    mixed = client.post(ASSIST, json={"query": f"查订单 {ORDER} 的物流，怎么申请退款？"})
    result = mixed.json()
    assert result["type"] == "policy_choice"
    assert result["operation"] == "logistics"
    assert result["order_id"] == ORDER
    assert ORDER not in result["query"]
    assert [choice["id"] for choice in result["choices"]] == ["knowledge", "logistics"]
    assert services.requests == []


@pytest.mark.parametrize("query", ["怎么退货？", "如何取消订单？", "如何修改收货地址？"])
def test_general_policy_about_an_unsupported_action_still_uses_existing_knowledge(commerce, query):
    services, client, _, _ = commerce
    response = client.post(ASSIST, json={"query": query})
    assert response.status_code == 200
    assert response.json() == {"type": "knowledge"}
    assert services.requests == []


def test_long_existing_knowledge_query_is_accepted(commerce):
    services, client, _, _ = commerce
    response = client.post(ASSIST, json={"query": "打印机如何连接电脑？" + "说明" * 1500})
    assert response.status_code == 200
    assert response.json() == {"type": "knowledge"}
    assert services.requests == []


@pytest.mark.parametrize("query", ["商品 P1001 蓝牙连不上怎么办", "查看商品 P1001 的使用说明"])
def test_product_id_does_not_divert_technical_questions(commerce, query):
    services, client, _, _ = commerce
    response = client.post(ASSIST, json={"query": query})
    assert response.status_code == 200
    assert response.json() == {"type": "knowledge"}
    assert services.requests == []


@pytest.mark.parametrize("query", ["帮我取消订单", "修改收货地址", "我的退款进度怎么样"])
def test_unsupported_personal_operations_do_not_fall_through_to_knowledge(commerce, query):
    services, client, _, _ = commerce
    response = client.post(ASSIST, json={"query": query})
    assert response.status_code == 200
    assert response.json()["type"] == "unsupported"
    assert services.requests == []


@pytest.mark.parametrize(("status", "code", "http_status", "expected"), [
    (404, "logistics_unavailable", 404, "logistics_unavailable"),
    (404, "commerce_not_found", 404, "commerce_not_found"),
    (401, "invalid_service_token", 502, "commerce_upstream_error"),
    (500, "internal_error", 503, "commerce_unavailable"),
    (200, "invalid_envelope", 502, "commerce_upstream_error"),
])
def test_upstream_read_failure_uses_stable_code_and_discards_detail(commerce, status, code, http_status, expected):
    services, client, _, _ = commerce
    services.read_response = httpx.Response(status, json={"code": code, "detail": "private-extra"})
    response = client.post(ASSIST, json={"operation": "logistics", "order_id": ORDER})
    assert response.status_code == http_status
    assert response.json()["code"] == expected
    assert "private-extra" not in response.text


@pytest.mark.parametrize(("failure", "http_status", "expected"), [
    (httpx.ConnectError, 503, "commerce_unavailable"),
    (httpx.ReadTimeout, 504, "commerce_timeout"),
])
def test_transport_errors_stay_in_commerce(commerce, monkeypatch, failure, http_status, expected):
    from app.commerce import gateway

    services, client, _, _ = commerce

    def send(request):
        if str(request.url) != VERIFY:
            raise failure("network details", request=request)
        return services.send(request)

    monkeypatch.setattr(gateway, "_http_client", lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(send),
    ))
    response = client.post(ASSIST, json={"operation": "order_status", "order_id": ORDER})
    assert response.status_code == http_status
    assert response.json()["code"] == expected
    assert "network details" not in response.text


@pytest.mark.parametrize("failure", [httpx.ConnectError, 503])
def test_read_can_recover_with_one_bounded_retry(commerce, monkeypatch, failure):
    from app.commerce import gateway

    services, client, _, _ = commerce
    attempts = []

    def send(request):
        attempts.append(request)
        if len(attempts) == 1:
            if failure == 503:
                return httpx.Response(503, headers={"Set-Cookie": "unexpected=service-value"})
            raise failure("connect failed", request=request)
        return services.send(request)

    monkeypatch.setattr(gateway, "_http_client", lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(send),
    ))
    response = client.post(ASSIST, json={"operation": "product_detail", "product_id": PRODUCT})
    assert response.status_code == 200
    assert response.json()["result"]["product_id"] == PRODUCT
    assert len(attempts) == 2
    assert all("cookie" not in request.headers for request in attempts)


def _preview(client):
    response = client.post(CONFIRMATIONS, json={
        "operation": "shipping_reminder", "order_id": ORDER, "note": "请尽快发货",
    })
    assert response.status_code == 200
    return response.json()


def test_confirmation_preview_does_not_write_and_is_bound_to_verified_subject(commerce):
    services, client, _, _ = commerce
    preview = _preview(client)
    assert preview["status"] == "pending"
    assert preview["preview"]["note"] == "请尽快发货"
    assert "receiver_address" not in preview["preview"]
    assert services.actions == []
    path = f"{CONFIRMATIONS}/{preview['confirmation_id']}"
    stranger = TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN},
                          cookies={"mall_session": "bob-session"})
    assert stranger.get(path).status_code == 404
    assert stranger.post(path + "/submit", json={}).status_code == 404
    assert services.actions == []
    assert client.get(path).json() == preview


def test_first_confirmation_expires_without_any_upstream_write(commerce):
    services, client, _, clock = commerce
    preview = _preview(client)
    clock[0] += timedelta(minutes=5)
    response = client.post(f"{CONFIRMATIONS}/{preview['confirmation_id']}/submit", json={})
    assert response.status_code == 410
    assert response.json()["code"] == "confirmation_expired"
    assert services.actions == []


def test_unknown_write_retries_same_key_and_replays_success(commerce):
    services, client, _, clock = commerce

    def action(request):
        if len(services.actions) == 1:
            raise httpx.ReadTimeout("response lost", request=request)
        return _success(ACTION_RESULT)

    services.action = action
    preview = _preview(client)
    confirmation_id = preview["confirmation_id"]
    assert str(UUID(confirmation_id)) == confirmation_id
    path = f"{CONFIRMATIONS}/{confirmation_id}"
    first = client.post(path + "/submit", json={})
    assert first.status_code == 503
    assert first.json()["code"] == "commerce_action_unknown"
    assert len(services.actions) == 1
    assert client.get(path).json()["status"] == "unknown"
    assert client.get(path).json()["retryable"] is True
    clock[0] += timedelta(minutes=8)
    second = client.post(path + "/submit", json={})
    assert second.status_code == 200
    receipt = second.json()
    assert receipt["status"] == "succeeded"
    assert receipt["result"] == {key: value for key, value in ACTION_RESULT.items() if key != "private_payload"}
    assert receipt["retryable"] is False
    assert client.post(path + "/submit", json={}).json() == receipt
    assert len(services.actions) == 2
    for request in services.actions:
        assert request.headers["idempotency-key"] == confirmation_id
        assert request.headers["x-commerce-user-id"] == "alice"
        assert json.loads(request.content) == {"note": "请尽快发货"}


def test_unknown_write_cannot_retry_after_original_retry_deadline(commerce):
    services, client, _, clock = commerce

    def action(request):
        raise httpx.ReadTimeout("response lost", request=request)

    services.action = action
    preview = _preview(client)
    path = f"{CONFIRMATIONS}/{preview['confirmation_id']}"
    assert client.post(path + "/submit", json={}).status_code == 503
    checkpoint = client.get(path).json()
    assert datetime.fromisoformat(checkpoint["retry_until"]) == clock[0] + timedelta(hours=24)
    clock[0] += timedelta(hours=24)
    response = client.post(path + "/submit", json={})
    assert response.status_code == 410
    assert response.json()["code"] == "confirmation_expired"
    assert client.get(path).json()["retryable"] is False
    assert len(services.actions) == 1


def test_transient_service_credentials_do_not_lose_an_unknown_receipt(commerce):
    services, client, _, _ = commerce

    def action(request):
        if len(services.actions) == 1:
            raise httpx.ReadTimeout("response lost after commit", request=request)
        if len(services.actions) == 2:
            return httpx.Response(401, json={"code": "invalid_service_token"})
        return _success(ACTION_RESULT)

    services.action = action
    preview = _preview(client)
    path = f"{CONFIRMATIONS}/{preview['confirmation_id']}"
    assert client.post(path + "/submit", json={}).status_code == 503
    assert client.post(path + "/submit", json={}).status_code == 503
    assert client.get(path).json()["status"] == "unknown"
    assert client.post(path + "/submit", json={}).json()["status"] == "succeeded"
    assert {request.headers["idempotency-key"] for request in services.actions} == {preview["confirmation_id"]}


def test_definitive_write_error_is_replayed_without_retry(commerce):
    services, client, _, _ = commerce
    services.action = lambda _: httpx.Response(400, json={
        "code": "shipping_ineligible", "detail": "private-upstream-info",
    })
    preview = _preview(client)
    path = f"{CONFIRMATIONS}/{preview['confirmation_id']}"
    for _ in range(2):
        response = client.post(path + "/submit", json={})
        assert response.status_code == 400
        assert response.json()["code"] == "shipping_ineligible"
        assert "private-upstream-info" not in response.text
    assert len(services.actions) == 1
    assert client.get(path).json()["status"] == "failed"


def test_concurrent_submit_does_not_issue_a_second_write(commerce):
    services, client, _, _ = commerce
    entered, release = Event(), Event()

    def action(_request):
        entered.set()
        assert release.wait(4)
        return _success(ACTION_RESULT)

    services.action = action
    preview = _preview(client)
    path = f"{CONFIRMATIONS}/{preview['confirmation_id']}"
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(client.post, path + "/submit", json={})
        try:
            assert entered.wait(4)
            other = client.post(path + "/submit", json={})
            assert other.status_code == 409
            assert client.get(path).json()["status"] == "submitting"
        finally:
            release.set()
        assert future.result(timeout=4).status_code == 200
    assert len(services.actions) == 1


def _task(client, session_id=DRAFT_SESSION):
    response = client.get(TASK, params={"session_id": session_id})
    assert response.status_code == 200
    return response.json()


def _turn(client, query, **options):
    response = client.post(ASSIST, json={"query": query, "session_id": DRAFT_SESSION, **options})
    assert response.status_code == 200
    return response.json()


def test_refund_carries_target_and_reason_across_turns_then_waits_for_submit(commerce):
    services, client, db, _ = commerce
    services.action = lambda _: _success(ACTION_RESULT | {"request_type": "refund_application"})

    assert _turn(client, "我要申请退款")["type"] == "choose"
    assert _task(client)["active"] == {"operation": "refund_application", "stage": "missing_target"}
    operation = _turn(client, ORDER, expect_task=True)
    assert operation["type"] == "operation"
    assert operation["order_id"] == ORDER
    assert _task(client)["active"] == {
        "operation": "refund_application", "stage": "missing_reason", "order_id": ORDER,
    }

    response = _turn(client, "买错了，不适合", expect_task=True)
    assert response["type"] == "confirmation"
    preview = response["confirmation"]
    assert preview["preview"]["reason"] == "买错了，不适合"
    assert preview["status"] == "pending"
    assert services.actions == []
    assert _task(client)["active"] == {
        "operation": "refund_application", "stage": "awaiting_confirmation", "order_id": ORDER,
        "confirmation_id": preview["confirmation_id"],
    }
    assert db[settings.mongo.chat_message_collection].count_documents({}) == 0

    submit = client.post(f"{CONFIRMATIONS}/{preview['confirmation_id']}/submit")
    assert submit.status_code == 200
    assert submit.json()["status"] == "succeeded"
    assert json.loads(services.actions[0].content) == {"reason": "买错了，不适合"}
    assert _task(client) == {"active": None, "paused": None}


def test_knowledge_and_reads_preserve_draft_and_resume_swaps_tasks(commerce):
    services, client, _, _ = commerce
    _turn(client, f"申请退款，订单 {ORDER}")
    current = _task(client)
    for query in ("怎么申请退款？", "打印机连不上怎么处理？", "查看路由器使用说明", "向量检索召回率", "你好", "谢谢"):
        assert _turn(client, query, expect_task=True) == {"type": "knowledge"}
        assert _task(client) == current
    assert _turn(client, "最近订单")["type"] == "result"
    assert _turn(client, f"查询订单 {ORDER} 的详情")["type"] == "result"
    assert _task(client) == current

    assert _turn(client, "帮我催发货")["type"] == "choose"
    switched = _task(client)
    assert switched["active"]["operation"] == "shipping_reminder"
    assert switched["paused"] == current["active"]
    assert _turn(client, "继续上一个任务")["type"] == "operation"
    assert _task(client) == {"active": current["active"], "paused": switched["active"]}
    assert _turn(client, "取消之前的任务")["type"] == "task_control"
    assert _task(client) == current
    assert _turn(client, "暂停当前任务")["type"] == "task_control"
    assert _task(client) == {"active": None, "paused": current["active"]}
    assert _turn(client, "继续之前的任务")["type"] == "operation"
    assert _turn(client, "取消当前任务")["type"] == "task_control"
    assert _task(client) == {"active": None, "paused": None}
    assert services.actions == []


def test_context_fills_only_compatible_missing_targets_without_hijacking_knowledge(commerce):
    _, client, _, _ = commerce
    reply = _turn(client, f"查订单 {ORDER} 的详情", order_id="O9999")
    assert reply["type"] == "result"
    assert reply["result"]["order_id"] == ORDER
    reply = _turn(client, "查找商品详情", product_id=PRODUCT)
    assert reply["type"] == "result"
    assert reply["result"]["product_id"] == PRODUCT
    assert _turn(client, "查看路由器使用说明", product_id=PRODUCT) == {"type": "knowledge"}
    reply = _turn(client, f"查订单 {ORDER} 和订单 O9999 的详情", order_id=ORDER)
    assert reply["type"] == "choose"
    assert reply["accepts_selection"] is False
    reply = _turn(client, f"查商品 {PRODUCT} 的物流", order_id=ORDER)
    assert reply["type"] == "choose"
    assert reply["operation"] == "logistics"
    assert reply["accepts_selection"] is False
    reply = _turn(client, "申请退款", product_id=PRODUCT)
    assert reply["type"] == "choose"
    assert reply["operation"] == "refund_application"
    reply = _turn(client, PRODUCT, expect_task=True)
    assert reply["type"] == "task_control"
    assert _task(client)["active"] == {"operation": "refund_application", "stage": "missing_target"}


def test_selected_objects_support_specific_references_without_stealing_usage_questions(commerce):
    _, client, _, _ = commerce
    order = _turn(client, "这个订单现在是什么情况？", order_id=ORDER)
    assert (order["type"], order["operation"], order["result"]["order_id"]) == ("result", "order_status", ORDER)
    product = _turn(client, "这件商品大概是什么情况？", product_id=PRODUCT)
    assert (product["type"], product["result"]["product_id"]) == ("result", PRODUCT)
    assert _turn(client, "这件商品如何使用？", product_id=PRODUCT) == {"type": "knowledge"}
    ambiguous = _turn(client, f"查订单 {ORDER} 和订单 O9999 的详情，怎么申请退款？", order_id=ORDER)
    assert ambiguous["type"] == "policy_choice"
    assert ambiguous["accepts_selection"] is False
    assert "order_id" not in ambiguous


def test_declarative_shipping_delay_can_be_a_reason_without_stealing_read_request(commerce):
    _, client, _, _ = commerce
    _turn(client, f"申请退款，订单 {ORDER}")
    assert _turn(client, "帮我查物流", expect_task=True)["type"] == "choose"
    assert _turn(client, "继续退款", expect_task=True)["type"] == "operation"
    confirmation = _turn(client, "物流太慢", expect_task=True)["confirmation"]
    assert confirmation["preview"]["reason"] == "物流太慢"


def test_task_identity_isolated_by_account_browser_and_conversation(commerce):
    _, client, _, _ = commerce
    _turn(client, "申请退款")
    scope = client.get(SESSION, params={"include_scope": True}).json()["account_scope"]
    assert scope and "alice" not in scope
    for account, owner in (("alice-session", "b" * 64), ("bob-session", "a" * 64)):
        other = TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN}, cookies={
            "mall_session": account, "kb_owner": owner,
        })
        assert _task(other) == {"active": None, "paused": None}
        assert other.get(SESSION, params={"include_scope": True}).json()["account_scope"] != scope
        assert _turn(other, "买错了", expect_task=True)["type"] == "task_control"
    assert _task(client, session_id="another-conversation") == {"active": None, "paused": None}
    assert _task(client)["active"]["operation"] == "refund_application"


def test_stale_task_reply_cannot_reach_knowledge_after_ttl_or_logout(commerce):
    _, client, _, clock = commerce
    _turn(client, f"我想申请退款，订单 {ORDER}")
    clock[0] += timedelta(minutes=30)
    assert _task(client) == {"active": None, "paused": None}
    assert _turn(client, "买错了", expect_task=True)["type"] == "task_control"
    assert _turn(client, ORDER, expect_task=True)["type"] == "task_control"
    assert _turn(client, "怎么申请退款？", expect_task=True) == {"type": "knowledge"}
    assert _turn(client, "你好", expect_task=True) == {"type": "knowledge"}
    assert _turn(client, "我的最近订单", expect_task=True)["type"] == "result"
    assert _turn(client, "催发货", expect_task=True)["type"] == "choose"

    guest = TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN})
    reply = guest.post(ASSIST, json={"query": "买错了", "session_id": DRAFT_SESSION, "expect_task": True})
    assert reply.status_code == 401
    assert reply.json()["code"] == "commerce_login_required"
    assert _turn(guest, "怎么申请退款？") == {"type": "knowledge"}
    assert _turn(guest, "怎么申请退款？", expect_task=True) == {"type": "knowledge"}


def test_objects_aggregate_only_verified_customers_sanitized_recent_data(commerce):
    services, client, _, _ = commerce
    reply = client.get(OBJECTS, headers={"X-Commerce-User-Id": "bob"})
    assert reply.status_code == 200
    assert reply.headers["cache-control"] == "no-store"
    payload = reply.json()
    assert set(payload) == {"orders", "products"}
    assert [item["order_id"] for item in payload["orders"]] == [ORDER]
    assert [item["product_id"] for item in payload["products"]] == [PRODUCT]
    assert "private" not in reply.text and "alice" not in reply.text
    assert {request.url.path for request in services.requests} == {
        "/verify", "/users/alice/orders", "/users/alice/products",
    }
    assert len(services.requests) == 3


def test_cancelled_confirmation_cannot_be_submitted_and_clears_draft(commerce):
    services, client, _, _ = commerce
    reply = client.post(CONFIRMATIONS, json={
        "operation": "shipping_reminder", "order_id": ORDER, "session_id": DRAFT_SESSION,
    })
    assert reply.status_code == 200
    preview = reply.json()
    assert _task(client)["active"]["confirmation_id"] == preview["confirmation_id"]
    path = f"{CONFIRMATIONS}/{preview['confirmation_id']}"
    stranger = TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN},
                          cookies={"mall_session": "bob-session"})
    assert stranger.post(path + "/cancel").status_code == 404
    assert client.post(path + "/cancel", headers={"Origin": "https://other.example.test"}).status_code == 403
    cancelled = client.post(path + "/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert client.get(path).json() == cancelled.json()
    submit = client.post(path + "/submit")
    assert submit.status_code == 409
    assert submit.json()["code"] == "confirmation_cancelled"
    assert _task(client) == {"active": None, "paused": None}
    assert services.actions == []


def test_cancel_cannot_override_a_claim_or_destroy_unknown_retry(commerce):
    services, client, _, _ = commerce
    entered, release = Event(), Event()

    def action(request):
        entered.set()
        assert release.wait(4)
        raise httpx.ReadTimeout("response lost", request=request)

    services.action = action
    path = f"{CONFIRMATIONS}/{_preview(client)['confirmation_id']}"
    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(client.post, path + "/submit")
        try:
            assert entered.wait(4)
            assert client.post(path + "/cancel").status_code == 409
        finally:
            release.set()
        assert first.result(timeout=4).status_code == 503
    assert client.post(path + "/cancel").status_code == 409
    assert client.get(path).json()["status"] == "unknown"
    services.action = lambda _: _success(ACTION_RESULT)
    assert client.post(path + "/submit").json()["status"] == "succeeded"
    assert len({request.headers["idempotency-key"] for request in services.actions}) == 1


def test_concurrent_previews_attach_only_one_pending_confirmation(commerce, monkeypatch):
    from app.commerce import gateway

    services, client, db, _ = commerce
    barrier = Barrier(2, timeout=4)
    original = gateway.order_detail

    async def concurrent_read(order_id, user_id):
        order = await original(order_id, user_id)
        barrier.wait()
        return order

    monkeypatch.setattr(gateway, "order_detail", concurrent_read)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(client.post, CONFIRMATIONS, json={
            "operation": "shipping_reminder", "order_id": ORDER, "session_id": DRAFT_SESSION, "note": note,
        }) for note in ("请尽快发货", "希望尽早发货")]
        responses = sorted((future.result(timeout=6) for future in futures), key=lambda response: response.status_code)
    winner, loser = responses
    assert winner.status_code == 200
    assert loser.status_code == 409
    assert loser.json()["code"] == "commerce_task_conflict"
    assert _task(client)["active"]["confirmation_id"] == winner.json()["confirmation_id"]
    assert sorted(doc["status"] for doc in db.commerce_confirmations.find()) == ["cancelled", "pending"]
    assert services.actions == []
