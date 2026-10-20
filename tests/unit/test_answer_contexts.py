"""The evaluation evidence must be the evidence actually sent to generation."""
from app.rag.query import answer_service
from app.process.query.agent.state import create_query_default_state


def test_prompt_records_exact_parent_and_web_evidence(monkeypatch):
    monkeypatch.setattr(answer_service, "build_history_context", lambda *a, **k: "历史")
    monkeypatch.setattr(answer_service, "load_prompt", lambda name, **kw: kw["context"])
    state = create_query_default_state(
        item_names=["HAK 180"], rewritten_query="如何设置",
        reranked_docs=[
            {"title": "设置", "text": "子块", "parent_content": "父章节", "page": 12, "score": .8},
            {"type": "web", "title": "网页", "text": "联网证据", "score": .6},
        ],
    )
    prompt = answer_service.load_answer_prompt(state)
    assert "父章节" in state["answer_contexts"][0]
    assert "联网证据" in state["answer_contexts"][1]
    assert "网络搜索" in state["answer_contexts"][1]
    assert "".join(state["answer_contexts"]) == prompt


def test_policy_excluded_parent_is_not_evaluation_evidence(monkeypatch):
    monkeypatch.setattr(answer_service, "load_prompt", lambda name, **kw: kw["context"])
    state = create_query_default_state(
        business_tag="pricing", reranked_docs=[
            {"text": "当前有效价格", "parent_content": "历史价格"},
        ],
    )
    answer_service.load_answer_prompt(state)
    assert "当前有效价格" in state["answer_contexts"][0]
    assert "历史价格" not in state["answer_contexts"][0]


def test_prefilled_answer_does_not_claim_unused_evidence(monkeypatch):
    monkeypatch.setattr(answer_service, "apply_business_policy", lambda state: None)
    monkeypatch.setattr(answer_service, "backfill_evolution_outputs", lambda state: state)
    monkeypatch.setattr(answer_service, "save_answer_message_history", lambda state: None)
    state = create_query_default_state(answer="请先选择产品", answer_contexts=["旧证据"])
    answer_service.generate_answer(state)
    assert state["answer_contexts"] == []


def test_answer_contexts_are_independent_between_queries():
    first = create_query_default_state()
    first["answer_contexts"].append("第一轮证据")
    assert create_query_default_state()["answer_contexts"] == []
