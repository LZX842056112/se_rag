"""Route explicit commerce requests without running a model or fetching private data."""
from __future__ import annotations

from dataclasses import dataclass
import re
from unicodedata import normalize

from app.commerce.models import AssistRequest, Operation, ORDER_TARGETS


_ID = r"([A-Za-z0-9][A-Za-z0-9_-]{0,63})(?![A-Za-z0-9_-])"
_ORDER_LABEL = re.compile(r"(?:订单(?:号|编号|ID)?|order\s+(?:id|number))\s*(?:[:#]|是)?\s*" + _ID, re.I)
_PRODUCT_LABEL = re.compile(
    r"(?:商品(?:号|编号|ID)?|product\s+(?:id|number)|SKU(?=[\s:#]))\s*(?:[:#]|是)?\s*" + _ID, re.I,
)
_ORDER_TOKEN = re.compile(r"(?<![A-Za-z0-9_-])(?=(?:ORD(?:ER)?[-_]?|O)\d)" + _ID, re.I)
_PRODUCT_TOKEN = re.compile(r"(?<![A-Za-z0-9_-])(?=(?:SKU[-_]?|P)\d)" + _ID, re.I)

_PERSONAL = re.compile(
    r"我的|我[这那]笔|我买的|我下的|我刚买|我已[经]?买|我[上前]次买|帮我|给我|为我|"
    r"\b(?:my|mine)\b", re.I,
)
_UNSUPPORTED = re.compile(
    r"取消.{0,8}订单|订单.{0,8}取消|退订订单|改.{0,8}(?:收货|配送|送货)地址|"
    r"(?:收货|配送|送货)地址.{0,8}(?:更改|修改|变更)|"
    r"退款.{0,8}(?:到哪|到账|进度|状态|结果|处理了|在哪里)|"
    r"退货(?!退款|政策|规则|流程|条件|怎么)|换货(?!政策|规则|流程|条件|怎么)|"
    r"修改.{0,8}订单|查看.{0,8}(?:收货地址|收件人|收货人|收货电话)|"
    r"购物车|(?:cancel|change|modify)\s+(?:my\s+|the\s+)?(?:order|delivery address)|"
    r"refund\s+(?:status|progress)", re.I,
)
_PRIVATE = re.compile(
    r"(?:(?:我的|本人|个人|我的商城)(?:账号|帐号|账户|手机|电话|地址|支付|银行卡|余额|优惠券|"
    r"购物车|交易|发票|积分|付款记录|收件人)|收货人|收货地址|收件人|收货电话|收货手机|配送地址|"
    r"个人订单|\bmy\s+(?:account|payment|card|address|phone|invoice|cart)\b)", re.I,
)
_POLICY_SIGNAL = re.compile(
    r"政策|规则|流程|步骤|条件|期限|时效|标准|一般|通常|怎么|怎样|如何|可以|可不可以|"
    r"能否|要多久|需要多久|多长时间|范围|什么意思|是什么|"
    r"\b(?:how|policy|procedure|requirements?)\b", re.I,
)
_CONTACT = re.compile(
    r"(?<!\d)1[3-9]\d{9}(?!\d)|"
    r"(?:手机号|电话|地址|收件人|收货人|邮箱)\s*[:=]\s*\S",
)
_TECHNICAL_USAGE = re.compile(
    r"使用说明|说明书|使用方法|用户手册|如何使用|怎么用|安装|连接不上|连不上|无法连接|"
    r"故障|设置|重置|恢复出厂|固件|驱动|维修|manual|troubleshoot|how to use", re.I,
)
_PRODUCT_LIVE_FIELDS = re.compile(
    r"现价|售价|价格|多少钱|库存|有货|缺货|在售|商品详情|price|in stock|availability", re.I,
)
_POLICIES = (
    (re.compile(r"退款|退货|换货|refund|return policy", re.I), "商城的退款政策和申请流程是什么？"),
    (re.compile(r"发货|催单|ship(?:ping)?", re.I), "商城的发货时效和发货提醒规则是什么？"),
    (re.compile(r"物流|快递|配送|delivery|tracking", re.I), "商城的配送和物流查询规则是什么？"),
    (re.compile(r"收货地址|送货地址|收件人|delivery address", re.I), "商城的收货信息管理规则是什么？"),
    (re.compile(r"订单|下单|order", re.I), "商城的下单和订单查询规则是什么？"),
)
_CLAUSE_BREAK = re.compile(r"[，。！？、；,!?;\n]+|顺便|同时|还有|以及|另外|\band\s+(?=how|what)\b", re.I)
_OPERATIONS: tuple[tuple[Operation, str], ...] = (
    ("shipping_reminder", r"催.{0,8}发货|催单|提醒.{0,8}发货|快点发货|urge.{0,12}ship|shipping reminder"),
    ("refund_application", r"(?:申请|提交|发起|办理|我要|想|帮我).{0,6}退款|退款申请|request.{0,8}refund|apply.{0,10}refund"),
    ("logistics", r"到哪[儿了里]?|物流|运单|快递|包裹|配送进度|什么时候到|几时到|到货了吗|"
                  r"多久.{0,6}送达|tracking|where.{0,16}order"),
    ("order_detail", r"订单.{0,16}(?:详情|明细|买了什么|金额|总价)|order.{0,10}(?:detail|items)"),
    ("order_status", r"订单.{0,16}(?:状态|进度)|发货[了没吗]|(?:何时|什么时候|几时)发货|"
                      r"是否(?:发货|付款)|order.{0,12}status"),
    ("recent_products", r"(?:最近|之前|上次|我).{0,8}(?:买[过的了]*|购买).{0,5}(?:商品|产品|什么|东西)|recent.{0,10}purchases"),
    ("order_list", r"最近.{0,8}订单|订单列表|历史订单|我的订单|\b(?:my|recent|list)\s+orders?\b"),
    ("product_detail", r"商品.{0,12}(?:详情|价格|库存|信息|规格)|(?:查(?:看|询)?|找).{0,5}商品|"
                       r"[这那](?:件|个|款)?商品.{0,12}(?:什么情况|啥情况|是什么|材质|怎么样)|"
                       r"有货|没货|库存|现价|售价|多少钱|in stock|product.{0,10}(?:price|detail)"),
    ("order_status", r"订单|order\s+(?:id|number)"),
)
_LIVE_PATTERNS = tuple((operation, re.compile(pattern, re.I)) for operation, pattern in _OPERATIONS)
_SPECIFIC_ORDER_STATUS = re.compile(r"[这那](?:笔|个|张)?订单.{0,16}(?:啥情况|什么情况|怎么样|状态|进度)")
_QUESTION = re.compile(r"[？?]|请问|想知道|什么|怎么|怎样|如何|为何|为什么|是否|哪里|几点|几个|多少|吗[。！!]*$|"
                       r"\b(?:what|when|where|why|how|which|can|could|should|does|is|are)\b", re.I)
_READ_REQUEST = re.compile(r"^(?:帮我|给我|请|我想)?\s*(?:查询|查看|查一下|看一下|查|看看|找一下|告诉我|了解)|"
                           r"^(?:check|show|find|tell)\b", re.I)
_REASON_LABEL = re.compile(r"^(?:退[款货](?:原因|理由)|原因|理由)\s*[:：是]\s*(.*)$", re.S)
_REASON_SIGNAL = re.compile(
    r"买错|下错|拍错|不[再想]要|不要了|不需|不喜欢|不合适|不满意|不合身|不符合|不符|不匹配|"
    r"损坏|破损|瑕疵|裂了|有故障|坏了|用不了|质量问题|质量差|质量太|缺货|没货|缺件|漏发|错发|发错|收错|"
    r"重复(?:购买|下单)|多买|太(?:大|小|贵|慢)|偏[大小]|[大小]了一码|改主意|换个|变更计划|个人原因|商品原因|"
    r"because|wrong item|damaged|broken|changed my mind|ordered by mistake|does not fit", re.I,
)
_TASK_CONTROLS = {
    "暂停": "pause", "暂停任务": "pause", "暂停当前任务": "pause",
    "继续": "resume", "继续任务": "resume", "恢复任务": "resume", "继续之前的任务": "resume",
    "继续上一个任务": "resume", "继续上个任务": "resume", "恢复之前的任务": "resume",
    "继续当前任务": "resume_current", "恢复当前任务": "resume_current",
    "取消任务": "cancel", "取消当前任务": "cancel", "放弃当前任务": "cancel",
    "取消之前的任务": "cancel_paused", "取消上一个任务": "cancel_paused",
}


@dataclass(frozen=True)
class Intent:
    operation: Operation | None = None
    order_id: str | None = None
    product_id: str | None = None
    policy_question: str | None = None
    unsupported: bool = False
    accepts_selection: bool = True


def task_control(query: str | None) -> tuple[str, Operation | None] | None:
    text = normalize("NFKC", query or "").strip().rstrip("。.!！")
    if text in _TASK_CONTROLS:
        return _TASK_CONTROLS[text], None
    if re.fullmatch(r"(?:继续|恢复)(?:申请)?退款(?:申请|任务)?", text):
        return "resume", "refund_application"
    if re.fullmatch(r"(?:继续|恢复)(?:催发货|催单|发货提醒)(?:任务)?", text):
        return "resume", "shipping_reminder"
    return None


def task_target(query: str | None) -> tuple[str, str | None] | None:
    text = normalize("NFKC", query or "").strip().rstrip("。.!！")
    for kind, label, token in (("order", _ORDER_LABEL, _ORDER_TOKEN), ("product", _PRODUCT_LABEL, _PRODUCT_TOKEN)):
        match = label.fullmatch(text) or token.fullmatch(text)
        if match:
            return match[1], kind
    if re.fullmatch(_ID, text) and (any(char.isdigit() for char in text) or not text.islower()):
        return text, None
    return None


def reason_has_label(query: str | None) -> bool:
    return _REASON_LABEL.fullmatch(query or "") is not None


def is_knowledge_question(query: str | None) -> bool:
    text = normalize("NFKC", query or "")
    return bool(_QUESTION.search(text) or _TECHNICAL_USAGE.search(text) or _policy_question(text))


def refund_reason(query: str | None) -> str | None:
    text = query or ""
    labeled = _REASON_LABEL.fullmatch(text)
    if labeled:
        return labeled[1].strip()
    if not (_QUESTION.search(text) or _READ_REQUEST.search(text) or _policy_question(text)) and _REASON_SIGNAL.search(text):
        return text
    return None


def _single_id(*matches: list[str]) -> str | None:
    distinct = {match for group in matches for match in group}
    return next(iter(distinct)) if len(distinct) == 1 else None


def _operation(text: str) -> Operation | None:
    return next((operation for operation, pattern in _LIVE_PATTERNS if pattern.search(text)), None)


def _policy_question(text: str) -> str | None:
    if _POLICY_SIGNAL.search(text):
        return next((question for topic, question in _POLICIES if topic.search(text)), None)
    return None


def interpret(body: AssistRequest) -> Intent:
    if body.operation is not None:
        return Intent(body.operation, body.order_id, body.product_id)

    assert body.query is not None
    text = normalize("NFKC", body.query)
    order_labels = _ORDER_LABEL.findall(text)
    order_tokens = _ORDER_TOKEN.findall(text)
    product_labels = _PRODUCT_LABEL.findall(text)
    product_tokens = _PRODUCT_TOKEN.findall(text)
    natural_order = _single_id(order_labels, order_tokens)
    natural_product = _single_id(product_labels, product_tokens)
    ambiguous_order = bool((order_labels or order_tokens) and natural_order is None)
    ambiguous_product = bool((product_labels or product_tokens) and natural_product is None)
    if body.session_id is None:
        order_id = body.order_id or natural_order
        product_id = body.product_id or natural_product
        explicit_order = bool(body.order_id or order_labels or order_tokens)
        explicit_product = bool(body.product_id or product_labels)
    else:
        order_id = natural_order or (body.order_id if not ambiguous_order else None)
        product_id = natural_product or (body.product_id if not ambiguous_product else None)
        explicit_order = bool(order_labels or order_tokens)
        explicit_product = bool(product_labels)

    # ponytail: keep live routing deterministic; extend these patterns when supported intents change.
    policy_question = None
    live_parts = []
    for clause in _CLAUSE_BREAK.split(text):
        question = None if body.session_id and _SPECIFIC_ORDER_STATUS.search(clause) else _policy_question(clause)
        if question:
            policy_question = policy_question or question
        if (
            not question or _PERSONAL.search(clause) or _ORDER_LABEL.search(clause)
            or _ORDER_TOKEN.search(clause) or _CONTACT.search(clause)
        ):
            live_parts.append(clause)

    live_text = " ".join(live_parts)
    if _UNSUPPORTED.search(live_text) or _PRIVATE.search(live_text):
        return Intent(unsupported=True)
    if policy_question and _CONTACT.search(live_text) and not _operation(live_text):
        return Intent(unsupported=True)
    operation = _operation(live_text)
    if (
        not explicit_order and operation in {None, "product_detail"}
        and _TECHNICAL_USAGE.search(text) and not _PRODUCT_LIVE_FIELDS.search(text)
    ):
        return Intent()
    if operation is None and explicit_order:
        operation = _operation(text) or "order_status"
    if operation is None and explicit_product:
        operation = "product_detail"
    if operation is None:
        return Intent(
            policy_question=policy_question,
            unsupported=bool(re.search(r"退款|催发货|交易记录|支付状态|refund", text, re.I) and not policy_question),
        )
    if explicit_order and operation == "order_list":
        operation = "order_status"

    accepts_selection = True
    if body.session_id is not None:
        has_product_id = bool(product_labels or product_tokens)
        if operation in ORDER_TARGETS:
            accepts_selection = not ambiguous_order and (explicit_order or not has_product_id)
        elif operation == "product_detail":
            accepts_selection = not ambiguous_product and (has_product_id or not explicit_order)
    return Intent(
        operation,
        order_id if operation in ORDER_TARGETS and accepts_selection else None,
        product_id if operation == "product_detail" and accepts_selection else None,
        policy_question,
        accepts_selection=accepts_selection,
    )
