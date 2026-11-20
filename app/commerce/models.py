"""Browser contracts and explicit allowlists for commerce service data."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
import math
from typing import Annotated, Literal, get_args
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    SecretStr,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    model_validator,
)


ReadOperation = Literal[
    "order_list", "recent_products", "product_detail", "order_detail", "order_status", "logistics",
]
Action = Literal["shipping_reminder", "refund_application"]
Operation = ReadOperation | Action
ACTIONS = frozenset(get_args(Action))
ORDER_TARGETS = frozenset({"order_detail", "order_status", "logistics", *ACTIONS})
CommerceId = Annotated[StrictStr, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")]
ConversationId = Annotated[StrictStr, Field(min_length=1, max_length=128)]
Money = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
ActionText = Annotated[StrictStr, Field(min_length=1, max_length=255)]
DEFAULT_REMINDER_NOTE = "用户希望尽快发货"


class BrowserRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class AccountLoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: CommerceId
    password: Annotated[SecretStr, Field(min_length=1, max_length=128)]


class AssistRequest(BrowserRequest):
    query: Annotated[StrictStr, Field(min_length=1, max_length=4096)] | None = None
    operation: Operation | None = None
    order_id: CommerceId | None = None
    product_id: CommerceId | None = None
    session_id: ConversationId | None = None
    expect_task: StrictBool = False

    @model_validator(mode="after")
    def validate_request(self) -> AssistRequest:
        if (self.query is None) == (self.operation is None):
            raise ValueError("请提供问题或操作")
        if self.expect_task and self.session_id is None:
            raise ValueError("请提供会话 ID")
        if self.order_id and self.product_id:
            raise ValueError("请只提供一个目标 ID")
        if self.operation is not None:
            if self.order_id and self.operation not in ORDER_TARGETS:
                raise ValueError("此操作不接受订单 ID")
            if self.product_id and self.operation != "product_detail":
                raise ValueError("此操作不接受商品 ID")
        return self


class ConfirmationRequest(BrowserRequest):
    operation: Action
    order_id: CommerceId
    note: ActionText | None = None
    reason: ActionText | None = None
    session_id: ConversationId | None = None

    @model_validator(mode="after")
    def validate_action_fields(self) -> ConfirmationRequest:
        if self.operation == "shipping_reminder" and self.reason is not None:
            raise ValueError("请在催发货请求中填写备注")
        if self.operation == "refund_application" and (self.note is not None or self.reason is None):
            raise ValueError("请填写退款原因")
        return self

    def action_payload(self) -> dict[str, str]:
        if self.operation == "shipping_reminder":
            return {"note": self.note or DEFAULT_REMINDER_NOTE}
        assert self.reason is not None
        return {"reason": self.reason}


class SubmitConfirmationRequest(BrowserRequest):
    pass


def _safe_cover_url(value: object) -> str | None:
    if not isinstance(value, str) or any(ord(char) <= 32 or char == "\\" for char in value):
        return None
    try:
        parts = urlsplit(value)
        if parts.scheme in {"https", "http"} and parts.hostname and parts.username is None:
            return value
    except ValueError:
        pass
    return None


CoverUrl = Annotated[str | None, BeforeValidator(_safe_cover_url)]
Attribute = StrictStr | StrictBool | StrictInt | StrictFloat


def _scalar_attributes(value: object) -> object:
    if not isinstance(value, dict):
        return value
    return {
        key: item for key, item in value.items()
        if type(item) in (str, bool, int) or (type(item) is float and math.isfinite(item))
    }


class OrderSummary(BaseModel):
    order_id: CommerceId
    title: StrictStr
    status: StrictStr
    amount: Money
    created_at: datetime
    cover_url: CoverUrl = None


class OrderList(BaseModel):
    user_id: CommerceId = Field(exclude=True)
    orders: list[OrderSummary]


class ProductSummary(BaseModel):
    product_id: CommerceId
    title: StrictStr
    price: Money
    cover_url: CoverUrl = None


class ProductList(BaseModel):
    user_id: CommerceId = Field(exclude=True)
    products: list[ProductSummary]


class ProductDetail(ProductSummary):
    description: StrictStr
    stock_status: StrictStr
    attributes: Annotated[dict[str, Attribute], BeforeValidator(_scalar_attributes)]


class OrderItem(BaseModel):
    product_id: CommerceId | Literal[""]
    title: StrictStr
    quantity: Annotated[StrictInt, Field(ge=0)]
    price: Money


class OrderStatus(BaseModel):
    order_id: CommerceId
    status: StrictStr
    status_desc: StrictStr


class OrderDetail(OrderStatus):
    amount: Money
    created_at: datetime
    items: list[OrderItem]


class LogisticsTrace(BaseModel):
    time: datetime
    desc: StrictStr


class Logistics(OrderStatus):
    logistics_company: StrictStr
    tracking_number: StrictStr
    traces: list[LogisticsTrace]


class ActionResult(OrderStatus):
    request_type: Action
    request_id: CommerceId


class SessionResponse(BaseModel):
    enabled: bool
    actions_enabled: bool
    authenticated: bool
    account_scope: str | None = None
    account_login_enabled: bool | None = None


class ObjectsResponse(BaseModel):
    orders: list[OrderSummary]
    products: list[ProductSummary]


class TaskDraft(BaseModel):
    operation: Operation
    stage: Literal["missing_target", "missing_reason", "ready_to_preview", "awaiting_confirmation"]
    order_id: CommerceId | None = None
    product_id: CommerceId | None = None
    confirmation_id: str | None = None


class TaskResponse(BaseModel):
    active: TaskDraft | None
    paused: TaskDraft | None


class Choice(BaseModel):
    id: str
    label: str


class KnowledgeResponse(BaseModel):
    type: Literal["knowledge"] = "knowledge"


class ResultResponse(BaseModel):
    type: Literal["result"] = "result"
    operation: ReadOperation
    result: OrderList | ProductList | ProductDetail | OrderDetail | OrderStatus | Logistics


class ChooseResponse(BaseModel):
    type: Literal["choose"] = "choose"
    operation: Operation
    choices: list[Choice]
    message: str | None = None
    accepts_selection: bool | None = None


class OperationResponse(BaseModel):
    type: Literal["operation"] = "operation"
    operation: Action
    order_id: CommerceId
    summary: OrderDetail


class PolicyChoiceResponse(BaseModel):
    type: Literal["policy_choice"] = "policy_choice"
    operation: Operation
    query: str
    order_id: CommerceId | None = None
    product_id: CommerceId | None = None
    choices: list[Choice]
    accepts_selection: bool | None = None


class UnsupportedResponse(BaseModel):
    type: Literal["unsupported"] = "unsupported"
    message: str


class TaskControlResponse(BaseModel):
    type: Literal["task_control"] = "task_control"
    message: str


class ConfirmationPreview(OrderDetail):
    note: str | None = None
    reason: str | None = None


class ConfirmationError(BaseModel):
    code: str
    message: str


class ConfirmationResponse(BaseModel):
    confirmation_id: str
    operation: Action
    order_id: CommerceId
    status: Literal["pending", "submitting", "unknown", "succeeded", "failed", "expired", "cancelled"]
    expires_at: datetime
    retry_until: datetime | None
    retryable: bool
    preview: ConfirmationPreview
    result: ActionResult | None = None
    error: ConfirmationError | None = None


class ConfirmationAssistResponse(BaseModel):
    type: Literal["confirmation"] = "confirmation"
    confirmation: ConfirmationResponse


AssistResponse = Annotated[
    KnowledgeResponse | ResultResponse | ChooseResponse | OperationResponse
    | PolicyChoiceResponse | UnsupportedResponse | TaskControlResponse | ConfirmationAssistResponse,
    Field(discriminator="type"),
]
