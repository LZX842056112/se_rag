"""HTTP 响应和 SSE 最终事件共用的答案字段。"""
from app.rag.query.citations import citations_from_reranked_docs


def answer_fields(state: dict) -> dict:
    """保留状态的原始空值；传输层继续处理自己的专用字段和校验。"""
    return {
        "answer": state.get("answer"),
        "answer_id": state.get("answer_id", ""),
        "business_tag": state.get("business_tag", ""),
        "handoff_required": state.get("handoff_required", False),
        "issue_id": state.get("issue_id", ""),
        "metrics": state.get("metrics", {}),
        "image_urls": state.get("image_urls", []),
        "item_names": state.get("item_names", []),
        "citations": state.get("citations") or citations_from_reranked_docs(state.get("reranked_docs")),
        "groundedness": state.get("groundedness"),
        "item_name_options": state.get("item_name_options", []),
        "intent": state.get("intent") or "rag",
    }
