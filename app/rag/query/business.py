"""RAG 业务标签和时效证据约束。不增加分类模型调用。"""
from __future__ import annotations

import math
import re
import time

# 业务标签 -> 触发关键词。分类走规则匹配，不额外调用分类模型（省成本、稳定可控）。
# 明确投诉优先。排查中的故障描述不视为投诉。
_COMPLAINT_WORDS = ("投诉", "骗人", "欺骗")
_TAG_WORDS = (
    ("complaint", (*_COMPLAINT_WORDS, "根本没解决", "一直没解决")),
    ("after_sales", ("售后", "退货", "退款", "保修", "质保", "发货", "订单", "物流", "购买")),
    ("pricing", ("价格", "多少钱", "报价", "库存", "现货", "折扣", "优惠", "售价")),
    ("troubleshooting", ("安装", "维修", "故障", "保养", "报错", "异常", "怎么用", "不工作", "怎么换", "排查", "不进纸", "无法进纸")),
)

# 排查后的联系条件属于说明书范围。只排除条件中的售后字样，保留同一问题中的真实政策诉求。
_SERVICE_ESCALATION = re.compile(
    r"((?:何时|什么时候|什么(?:情况|条件|程度)(?:下)?|是否(?:要|应|需要|必须)?|仍(?:然)?(?:无法|不能)解决|还没解决)"
    r"[，,]?[^，。！？；,.!?;\n]{0,18})(?:联系|咨询|找|拨打|打)\s*(?:官方|厂家|厂商|品牌)?\s*售后(?:服务|支持|人员|客服|电话)?"
)


def classify_business(query: str) -> str:
    """按关键词给问题打业务标签；无关键字命中则归为普通产品知识。"""
    troubleshooting = any(word in query for word in _TAG_WORDS[-1][1])
    if troubleshooting:
        query = _SERVICE_ESCALATION.sub(r"\1", query)
    for tag, words in _TAG_WORDS:
        if tag == "complaint" and troubleshooting:
            words = _COMPLAINT_WORDS
        if any(word in query for word in words):
            return tag  # 首个命中的标签
    return "product_knowledge"


def classify_business_request(state: dict) -> str:
    """根据原句、点选意图与改写结果确定检索和作答的业务范围。"""
    user_query = "\n".join(str(state.get(key) or "") for key in ("original_query", "rewrite_hint"))
    tag = classify_business(user_query)
    if tag == "product_knowledge":
        tag = classify_business(state.get("rewritten_query") or "")
    return tag


def policy_supported(docs: list[dict], now: float | None = None) -> bool:
    """判断给定文档里是否至少有一份「当前有效」的业务政策。"""
    return any(is_current_policy(doc, now) for doc in docs)


def is_current_policy(doc: dict, now: float | None = None) -> bool:
    """单份文档是否为「仍在有效期内」的业务政策。

    要求：类型是 policy、有版本号、有效期 ``valid_until`` 为有限数值且在 now 之后——
    三条件都满足才算可用，防止用过期/未打版本的政策作答。
    """
    now = time.time() if now is None else now
    try:
        until = float(doc.get("valid_until") or 0)
    except (ValueError, TypeError):
        return False  # 有效期字段缺失或非法，一律视为不可用
    return (doc.get("document_kind") == "policy" and bool(doc.get("document_version"))
            and math.isfinite(until) and until > now)


def apply_business_policy(state: dict) -> None:
    """按业务标签改写 state：投诉转人工、价格/售后只允许有效政策作答。

    - complaint：一律转人工，清空检索文档；
    - pricing/after_sales：只保留当前有效的政策文档，无有效政策则不给答案转人工。
    前序主体兜底（item_name_options）除投诉外不阻断正常作答。
    """
    tag = classify_business_request(state)
    state["business_tag"] = tag
    # 有可点选主体候选时，先让用户确认主体，不急于按业务标签接管（投诉除外）
    if state.get("item_name_options") and tag != "complaint":
        return
    if tag == "complaint":
        # 投诉：人工介入，要求补充产品、经过与诉求
        state["answer"] = "这个问题需要负责人处理。请补充产品、问题经过和期望的处理结果。"
        state["handoff_required"] = True
        state["issue_type"] = "complaint"
        state["reranked_docs"] = []
    elif tag in ("pricing", "after_sales"):
        # 价格/售后：只你信任「当前有效」的政策文档，避免拿过期资料误导
        policies = [doc for doc in state.get("reranked_docs", []) if is_current_policy(doc)]
        if not policies:
            state["answer"] = "当前没有有效的业务政策资料，不能确认价格、库存或售后承诺。请交由负责人核实。"
            state["handoff_required"] = True
            state["issue_type"] = "missing_material"
            state["reranked_docs"] = []
        else:
            # 有有效政策：只留政策文档作为作答依据
            state["reranked_docs"] = policies
            # 前序主体兜底不阻止有效业务政策作答。
            if state.get("answer") and not state.get("item_names"):
                state["answer"] = ""


def needs_business_route(query: str) -> bool:
    """判断是否要进入业务策略流程：命中业务标签，或含指向代词/前述对象的问题。

    后者（怎么/如何/为什么 + 它/这个/那个）往往指代上文，直接走普通检索容易答非所问。
    """
    return classify_business(query) != "product_knowledge" or bool(re.search(r"(怎么|如何|为什么).*(它|这个|那个)", query))
