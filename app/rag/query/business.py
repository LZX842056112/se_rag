"""RAG 业务标签和时效证据约束。不增加分类模型调用。"""
from __future__ import annotations

import math
import re
import time

_TAG_WORDS = (
    ("complaint", ("投诉", "骗人", "欺骗", "根本没解决", "一直没解决")),
    ("after_sales", ("售后", "退货", "退款", "保修", "质保", "发货", "订单", "物流", "购买")),
    ("pricing", ("价格", "多少钱", "报价", "库存", "现货", "折扣", "优惠", "售价")),
    ("troubleshooting", ("安装", "维修", "故障", "保养", "报错", "异常", "怎么用", "不工作", "怎么换")),
)


def classify_business(query: str) -> str:
    for tag, words in _TAG_WORDS:
        if any(word in query for word in words):
            return tag
    return "product_knowledge"


def policy_supported(docs: list[dict], now: float | None = None) -> bool:
    return any(is_current_policy(doc, now) for doc in docs)


def is_current_policy(doc: dict, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    try:
        until = float(doc.get("valid_until") or 0)
    except (ValueError, TypeError):
        return False
    return (doc.get("document_kind") == "policy" and bool(doc.get("document_version"))
            and math.isfinite(until) and until > now)


def apply_business_policy(state: dict) -> None:
    context = " ".join(str(state.get(key) or "") for key in ("original_query", "rewrite_hint", "rewritten_query"))
    tag = classify_business(context)
    state["business_tag"] = tag
    if state.get("item_name_options") and tag != "complaint":
        return
    if tag == "complaint":
        state["answer"] = "这个问题需要负责人处理。请补充产品、问题经过和期望的处理结果。"
        state["handoff_required"] = True
        state["issue_type"] = "complaint"
        state["reranked_docs"] = []
    elif tag in ("pricing", "after_sales"):
        policies = [doc for doc in state.get("reranked_docs", []) if is_current_policy(doc)]
        if not policies:
            state["answer"] = "当前没有有效的业务政策资料，不能确认价格、库存或售后承诺。请交由负责人核实。"
            state["handoff_required"] = True
            state["issue_type"] = "missing_material"
            state["reranked_docs"] = []
        else:
            state["reranked_docs"] = policies
            # 前序主体兜底不阻止有效业务政策作答。
            if state.get("answer") and not state.get("item_names"):
                state["answer"] = ""


def needs_business_route(query: str) -> bool:
    return classify_business(query) != "product_knowledge" or bool(re.search(r"(怎么|如何|为什么).*(它|这个|那个)", query))
