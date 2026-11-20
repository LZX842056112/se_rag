"""Commerce endpoints for the chat page."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace
from typing import TypeVar
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import APIRouter, FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pymongo import timeout
from pymongo.errors import PyMongoError
from starlette._utils import get_route_path
from starlette.concurrency import run_in_threadpool

from app.api.errors import ApiError, error_body
from app.commerce import confirmations, gateway, tasks
from app.commerce.intent import (
    Intent, interpret, is_knowledge_question, reason_has_label, refund_reason, task_control, task_target,
)
from app.commerce.models import (
    ACTIONS,
    AccountLoginRequest,
    AssistRequest,
    AssistResponse,
    Choice,
    ChooseResponse,
    ConfirmationAssistResponse,
    ConfirmationRequest,
    ConfirmationResponse,
    ConversationId,
    KnowledgeResponse,
    ObjectsResponse,
    Operation,
    OperationResponse,
    PolicyChoiceResponse,
    ResultResponse,
    SessionResponse,
    SubmitConfirmationRequest,
    TaskControlResponse,
    TaskDraft,
    TaskResponse,
    UnsupportedResponse,
)
from app.rag.query.greetings import is_plain_greeting
from app.shared.config import settings
from app.shared.runtime.logger import logger


_PREFIX = "/api/commerce"
_T = TypeVar("_T")
_OPERATION_LABELS = {
    "order_list": "查看最近订单",
    "recent_products": "查看最近购买的商品",
    "product_detail": "查看商品详情",
    "order_detail": "查看订单详情",
    "order_status": "查看订单状态",
    "logistics": "查看订单物流",
    "shipping_reminder": "准备催发货",
    "refund_application": "准备退款申请",
}
router = APIRouter(prefix=_PREFIX, tags=["commerce"])


def install_commerce_boundary(app: FastAPI) -> None:
    @app.middleware("http")
    async def protect_commerce_requests(request: Request, call_next):
        path = get_route_path(request.scope)
        if path != _PREFIX and not path.startswith(_PREFIX + "/"):
            return await call_next(request)

        origin = settings.commerce.public_origin
        if request.method == "POST" and (not origin or request.headers.getlist("origin") != [origin]):
            response = JSONResponse(
                status_code=403,
                content=error_body("commerce_origin_forbidden", "请求来源不允许访问商城接口"),
            )
        else:
            try:
                response = await call_next(request)
            except Exception as exc:
                logger.error("商城接口内部错误 ({})", type(exc).__name__)
                response = JSONResponse(
                    status_code=500, content=error_body("internal_error", "商城服务内部错误"),
                )
        response.headers["Cache-Control"] = "no-store"
        return response


def _require_enabled(*, actions: bool = False) -> None:
    if not settings.commerce.enabled:
        raise ApiError("commerce_unavailable", "商城客服暂不可用", 503)
    if actions and not settings.commerce.actions_enabled:
        raise ApiError("commerce_unavailable", "商城操作暂不可用", 503)


async def _user_id(request: Request) -> str:
    return await gateway.verify_identity(request.cookies.get(settings.commerce.session_cookie_name))


async def _optional_user(request: Request) -> str | None:
    try:
        return await _user_id(request)
    except ApiError as exc:
        if exc.code == "commerce_login_required":
            return None
        raise


def _account_scope(request: Request, user_id: str) -> str:
    owner = getattr(request.state, "owner_token", "")
    if not owner:
        raise ApiError("session_not_found", "会话不可访问，请重新打开客服页面", 404)
    return tasks.account_scope(owner, user_id)


@router.get("/session", response_model_exclude_none=True)
async def session(request: Request, include_scope: bool = False) -> SessionResponse:
    enabled = settings.commerce.enabled
    user_id = await _optional_user(request) if enabled else None
    return SessionResponse(
        enabled=enabled,
        actions_enabled=enabled and settings.commerce.actions_enabled,
        authenticated=user_id is not None,
        account_scope=_account_scope(request, user_id) if include_scope and user_id else None,
        account_login_enabled=True if enabled and settings.commerce.account_login_enabled else None,
    )


def _account_cookie(request: Request) -> dict:
    _require_enabled()
    if not settings.commerce.account_login_enabled:
        raise ApiError("commerce_login_unavailable", "请在商城登录。", 404)
    origin = urlsplit(settings.commerce.public_origin)
    secure = origin.scheme == "https"
    if not secure and (
        origin.scheme != "http" or origin.hostname not in {"127.0.0.1", "localhost", "::1"}
        or settings.api.app_env == "prod"
    ):
        raise ApiError("commerce_login_unavailable", "商城登录需要 HTTPS。", 503)
    return {
        "key": settings.commerce.session_cookie_name,
        "path": request.scope.get("root_path", "").rstrip("/") + "/",
        "httponly": True,
        "secure": secure,
        "samesite": "strict",
    }


@router.post("/login")
async def account_login(body: AccountLoginRequest, request: Request, response: Response) -> dict[str, bool]:
    cookie = _account_cookie(request)
    token, max_age = await gateway.open_customer_session(
        body.user_id, body.password.get_secret_value(), request.cookies.get(cookie["key"]),
    )
    response.set_cookie(value=token, max_age=max_age, **cookie)
    return {"authenticated": True}


@router.post("/logout")
async def account_logout(request: Request, response: Response) -> dict[str, bool]:
    cookie = _account_cookie(request)
    await gateway.close_customer_session(request.cookies.get(cookie["key"]))
    response.delete_cookie(**cookie)
    return {"authenticated": False}


@router.get("/objects")
async def objects(request: Request) -> ObjectsResponse:
    _require_enabled()
    user_id = await _user_id(request)
    orders, products = await asyncio.gather(gateway.order_list(user_id), gateway.recent_products(user_id))
    return ObjectsResponse(orders=orders.orders, products=products.products)


@router.get("/task", response_model_exclude_unset=True)
async def task(request: Request, session_id: ConversationId) -> TaskResponse:
    _require_enabled()
    snapshot = await _task_snapshot(request, await _user_id(request), session_id)
    return TaskResponse(active=snapshot.active, paused=snapshot.paused)


@router.post("/assist", response_model_exclude_none=True)
async def assist(body: AssistRequest, request: Request) -> AssistResponse:
    _require_enabled()
    intent = interpret(body)
    if body.session_id is not None:
        response = await _assist_task(body, intent, request)
        if response is not None:
            return response
    return await _render_assist(intent, await _intent_user(intent, request))


async def _intent_user(intent: Intent, request: Request) -> str | None:
    operation = intent.operation
    if intent.unsupported or operation is None or intent.policy_question:
        return None
    if operation in ACTIONS:
        _require_enabled(actions=True)
    if operation == "product_detail":
        return await _optional_user(request) if intent.product_id is None else None
    return await _user_id(request)


async def _render_assist(intent: Intent, user_id: str | None) -> AssistResponse:
    operation = intent.operation
    if intent.unsupported:
        return UnsupportedResponse(message="请在商城处理此请求。")
    if operation is None:
        return KnowledgeResponse()
    accepts_selection = None if intent.accepts_selection else False
    if intent.policy_question is not None:
        return PolicyChoiceResponse(
            operation=operation,
            query=intent.policy_question,
            order_id=intent.order_id,
            product_id=intent.product_id,
            accepts_selection=accepts_selection,
            choices=[
                Choice(id="knowledge", label="查看商城规则"),
                Choice(id=operation, label=_OPERATION_LABELS[operation]),
            ],
        )
    if operation == "product_detail":
        if intent.product_id is not None:
            product = await gateway.product_detail(intent.product_id)
            return ResultResponse(operation=operation, result=product)
        products = (await gateway.recent_products(user_id)).products if user_id else []
        return ChooseResponse(
            operation=operation,
            choices=[Choice(id=item.product_id, label=item.title) for item in products],
            message="请选择最近购买的商品，或输入商品 ID。" if products else "请输入商品 ID。",
            accepts_selection=accepts_selection,
        )

    assert user_id is not None
    if operation == "order_list":
        return ResultResponse(operation=operation, result=await gateway.order_list(user_id))
    if operation == "recent_products":
        return ResultResponse(operation=operation, result=await gateway.recent_products(user_id))
    if intent.order_id is None:
        orders = (await gateway.order_list(user_id)).orders
        return ChooseResponse(
            operation=operation,
            choices=[Choice(id=item.order_id, label=item.title) for item in orders],
            message="请选择订单，或输入订单 ID。" if orders else "请输入订单 ID。",
            accepts_selection=accepts_selection,
        )
    if operation in ACTIONS:
        order = await gateway.order_detail(intent.order_id, user_id)
        return OperationResponse(operation=operation, order_id=intent.order_id, summary=order)
    return ResultResponse(
        operation=operation, result=await gateway.order_read(operation, intent.order_id, user_id),
    )


async def _store(method: Callable[..., _T], *args, **kwargs) -> _T:
    def run() -> _T:
        with timeout(5):
            return method(*args, **kwargs)

    try:
        return await run_in_threadpool(run)
    except (PyMongoError, ValueError) as exc:
        raise ApiError("commerce_unavailable", "商城任务服务暂不可用", 503) from exc


async def _task_snapshot(request: Request, user_id: str, session_id: str) -> tasks.Snapshot:
    task_key = tasks.key(_account_scope(request, user_id), session_id)
    return await _store(tasks.load, task_key, user_id)


def _matching_draft(snapshot: tasks.Snapshot, intent: Intent) -> TaskDraft | None:
    if intent.accepts_selection:
        for draft in (snapshot.active, snapshot.paused):
            if draft is None or draft.operation != intent.operation:
                continue
            if draft is snapshot.paused and not (
                intent.order_id and draft.order_id == intent.order_id
                or intent.product_id and draft.product_id == intent.product_id
            ):
                continue
            if (not intent.order_id or not draft.order_id or intent.order_id == draft.order_id) and (
                not intent.product_id or not draft.product_id or intent.product_id == draft.product_id
            ):
                return draft
    return None


def _paused_after_activation(snapshot: tasks.Snapshot, matched: TaskDraft | None) -> TaskDraft | None:
    if matched is snapshot.active:
        return snapshot.paused
    if matched is not None and matched is snapshot.paused:
        return snapshot.active
    if snapshot.active is not None and snapshot.paused is not None:
        raise ApiError("commerce_task_full", "请先完成或取消一个任务，再开始新任务。", 409)
    return snapshot.active or snapshot.paused


async def _render_draft(draft: TaskDraft, user_id: str) -> AssistResponse:
    if draft.confirmation_id is not None:
        doc = await _store(confirmations.get, user_id, draft.confirmation_id)
        return ConfirmationAssistResponse(confirmation=ConfirmationResponse.model_validate(confirmations.view(doc)))
    if draft.operation in ACTIONS:
        _require_enabled(actions=True)
    return await _render_assist(
        Intent(draft.operation, draft.order_id, draft.product_id, accepts_selection=False), user_id,
    )


async def _control_task(
    snapshot: tasks.Snapshot, user_id: str, command: str, operation: Operation | None,
) -> AssistResponse:
    active, paused = snapshot.active, snapshot.paused
    if command.startswith("resume"):
        candidates = (active,) if command == "resume_current" else (paused, active)
        chosen = next((draft for draft in candidates if draft and (operation is None or draft.operation == operation)), None)
        if chosen is None:
            return TaskControlResponse(message="没有可以继续的任务。")
        reply = await _render_draft(chosen, user_id)
        if chosen is paused:
            await _store(tasks.save, snapshot, paused, active)
        return reply
    if command == "pause":
        if active is None:
            return TaskControlResponse(message="没有可以暂停的任务。")
        if paused is not None:
            return TaskControlResponse(message="已有暂停的任务。请先继续或取消该任务。")
        await _store(tasks.save, snapshot, None, active)
        return TaskControlResponse(message="已暂停当前任务。")

    cancel_paused = command == "cancel_paused"
    chosen = paused if cancel_paused else active
    if chosen is None:
        return TaskControlResponse(message="没有可以取消的任务。")
    if chosen.confirmation_id is not None:
        await _store(confirmations.cancel, user_id, chosen.confirmation_id)
    await _store(tasks.save, snapshot, active if cancel_paused else None, None if cancel_paused else paused)
    return TaskControlResponse(message="已取消任务。")


async def _assist_task(body: AssistRequest, intent: Intent, request: Request) -> AssistResponse | None:
    assert body.session_id is not None
    if body.query and is_plain_greeting(body.query):
        return None
    command = task_control(body.query)
    target = task_target(body.query)
    reason = refund_reason(body.query)
    labeled_reason = reason_has_label(body.query)
    live = intent.operation is not None and not intent.policy_question and not intent.unsupported
    likely_reply = target is not None or reason is not None
    if not command and not labeled_reason:
        if intent.policy_question or (intent.unsupported and not likely_reply):
            return None
        if not live and not likely_reply and (not body.expect_task or is_knowledge_question(body.query)):
            return None
    expect_reply = body.expect_task and (likely_reply or not live)
    if live and intent.operation in ACTIONS and command is None:
        _require_enabled(actions=True)
    if command or expect_reply or labeled_reason or (live and intent.operation != "product_detail"):
        user_id = await _user_id(request)
    else:
        user_id = await _optional_user(request)
    if user_id is None:
        return await _render_assist(intent, None)

    snapshot = await _task_snapshot(request, user_id, body.session_id)
    if command:
        return await _control_task(snapshot, user_id, *command)
    active = snapshot.active
    if expect_reply and active is None:
        return TaskControlResponse(message="当前任务已失效。请继续已暂停的任务，或重新发起任务。")

    if active and active.stage == "missing_target" and target is not None:
        target_id, kind = target
        product = active.operation == "product_detail"
        if kind is not None and (kind == "product") != product:
            name = "商品" if product else "订单"
            return TaskControlResponse(message=f"请为当前任务选择{name}，或输入{name} ID。")
        intent = Intent(active.operation, product_id=target_id) if product else Intent(active.operation, order_id=target_id)
    elif (active and active.stage == "missing_reason" and reason is not None
          and (labeled_reason or intent.operation not in ACTIONS)):
        _require_enabled(actions=True)
        if not 1 <= len(reason) <= 255:
            return TaskControlResponse(message="请输入 1 至 255 个字符的退款原因。")
        assert active.order_id is not None
        preview = await _create_preview(user_id, ConfirmationRequest(
            operation="refund_application", order_id=active.order_id, reason=reason,
        ), snapshot)
        return ConfirmationAssistResponse(confirmation=preview)
    elif labeled_reason or (active and active.operation == "refund_application" and not live and reason is not None):
        if active and active.operation == "refund_application" and active.stage == "awaiting_confirmation":
            return await _render_draft(active, user_id)
        return TaskControlResponse(message="请先选择退款订单，再输入原因。")
    if intent.operation in ACTIONS:
        _require_enabled(actions=True)
    return await _apply_task_intent(intent, snapshot, user_id)


async def _apply_task_intent(intent: Intent, snapshot: tasks.Snapshot, user_id: str) -> AssistResponse:
    matched = _matching_draft(snapshot, intent)
    if matched is not None:
        intent = replace(intent, order_id=intent.order_id or matched.order_id,
                         product_id=intent.product_id or matched.product_id)
        if matched.confirmation_id is not None:
            response = await _render_draft(matched, user_id)
            if matched is snapshot.paused:
                await _store(tasks.save, snapshot, matched, snapshot.active)
            return response
    response = await _render_assist(intent, user_id)
    draft = None
    if isinstance(response, ChooseResponse) and intent.accepts_selection:
        draft = TaskDraft(operation=response.operation, stage="missing_target")
    elif isinstance(response, OperationResponse):
        draft = TaskDraft(
            operation=response.operation, order_id=response.order_id,
            stage="missing_reason" if response.operation == "refund_application" else "ready_to_preview",
        )
    if draft is not None:
        await _store(tasks.save, snapshot, draft, _paused_after_activation(snapshot, matched))
    elif isinstance(response, ResultResponse) and matched is snapshot.active and matched is not None:
        await _store(tasks.save, snapshot, None, snapshot.paused)
    return response


@router.post("/confirmations", response_model_exclude_unset=True)
async def preview_confirmation(body: ConfirmationRequest, request: Request) -> ConfirmationResponse:
    _require_enabled(actions=True)
    user_id = await _user_id(request)
    snapshot = await _task_snapshot(request, user_id, body.session_id) if body.session_id is not None else None
    return await _create_preview(user_id, body, snapshot)


async def _create_preview(
    user_id: str, body: ConfirmationRequest, snapshot: tasks.Snapshot | None,
) -> ConfirmationResponse:
    paused = None
    if snapshot is not None:
        matched = _matching_draft(snapshot, Intent(body.operation, body.order_id))
        if matched and matched.confirmation_id is not None:
            doc = await _store(confirmations.get, user_id, matched.confirmation_id)
            if matched is snapshot.paused:
                await _store(tasks.save, snapshot, matched, snapshot.active)
            return ConfirmationResponse.model_validate(confirmations.view(doc))
        paused = _paused_after_activation(snapshot, matched)
    order = await gateway.order_detail(body.order_id, user_id)
    doc = await _store(confirmations.create, user_id, body, order)
    if snapshot is not None:
        draft = TaskDraft(operation=body.operation, stage="awaiting_confirmation", order_id=body.order_id,
                          confirmation_id=doc["_id"])
        try:
            await _store(tasks.save, snapshot, draft, paused)
        except ApiError as exc:
            if exc.code == "commerce_task_conflict":
                await _store(confirmations.cancel, user_id, doc["_id"])
            raise
    return ConfirmationResponse.model_validate(confirmations.view(doc))


@router.get("/confirmations/{confirmation_id}", response_model_exclude_unset=True)
async def confirmation(confirmation_id: UUID, request: Request) -> ConfirmationResponse:
    _require_enabled()
    user_id = await _user_id(request)
    doc = await _store(confirmations.get, user_id, str(confirmation_id))
    return ConfirmationResponse.model_validate(confirmations.view(doc))


@router.post("/confirmations/{confirmation_id}/cancel", response_model_exclude_unset=True)
async def cancel_confirmation(confirmation_id: UUID, request: Request) -> ConfirmationResponse:
    _require_enabled()
    user_id = await _user_id(request)
    doc = await _store(confirmations.cancel, user_id, str(confirmation_id))
    return ConfirmationResponse.model_validate(confirmations.view(doc))


def _unknown() -> ApiError:
    return ApiError("commerce_action_unknown", "提交结果暂不明确。请查看或重试此确认请求。", 503)


def _submission_response(doc: dict) -> ConfirmationResponse:
    if doc["status"] == "succeeded":
        return ConfirmationResponse.model_validate(confirmations.view(doc))
    if doc["status"] == "failed":
        error = doc["error"]
        raise ApiError(error["code"], error["message"], error["status_code"])
    if doc["status"] == "cancelled":
        raise ApiError("confirmation_cancelled", "确认请求已取消。", 409)
    raise _unknown()


@router.post("/confirmations/{confirmation_id}/submit", response_model_exclude_unset=True)
async def submit_confirmation(
    confirmation_id: UUID, request: Request, _body: SubmitConfirmationRequest | None = None,
) -> ConfirmationResponse:
    _require_enabled(actions=True)
    user_id = await _user_id(request)
    confirmation_key = str(confirmation_id)
    doc, lease = await _store(confirmations.claim, user_id, confirmation_key)
    if lease is None:
        return _submission_response(doc)

    result = None
    error = None
    try:
        result = await gateway.submit_action(
            doc["operation"], doc["order_id"], user_id, confirmation_key, doc["payload"],
        )
    except gateway.ActionRejected as exc:
        error = exc
    except ApiError:
        pass

    try:
        completed = await _store(
            confirmations.complete, user_id, confirmation_key, lease, result=result, error=error,
        )
    except ApiError as exc:
        raise _unknown() from exc
    return _submission_response(completed)
