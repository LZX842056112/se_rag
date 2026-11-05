"""RAG 业务标签和时效证据约束。不增加分类模型调用。"""
from __future__ import annotations

import math
import re
import time

# 业务标签 -> 触发关键词。分类走规则匹配，不额外调用分类模型（省成本、稳定可控）。
# 顺序即优先级：complaint 在最前，命中即返回，避免被售后/价格等词抢先误判。
_TAG_WORDS = (
    ("complaint", ("投诉", "骗人", "欺骗", "根本没解决", "一直没解决")),
    ("after_sales", ("售后", "退货", "退款", "保修", "质保", "发货", "订单", "物流", "购买")),
    ("pricing", ("价格", "多少钱", "报价", "库存", "现货", "折扣", "优惠", "售价")),
    ("troubleshooting", ("安装", "维修", "故障", "保养", "报错", "异常", "怎么用", "不工作", "怎么换")),
)


def classify_business(query: str) -> str:
    """按关键词给问题打业务标签；无关键字命中则归为普通产品知识。"""
    for tag, words in _TAG_WORDS:
        if any(word in query for word in words):
            return tag  # 首个命中的标签
    return "product_knowledge"


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
    # 用多种来源拼出判定文本，覆盖「原句 / 点选意图 / 改写后」任一形式
    context = " ".join(str(state.get(key) or "") for key in ("original_query", "rewrite_hint", "rewritten_query"))
    tag = classify_business(context)
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