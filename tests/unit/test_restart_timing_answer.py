import json
from types import SimpleNamespace

import pytest

from app.rag.query import answer_service
from app.rag.agentic.controller import run_agentic
from app.rag.agentic.evidence import doc_to_evidence
from app.rag.query.rerank_service import deal_rrf_and_web_result


QUESTION = "Aolynk CB304n 网桥重启后会立即生效吗？"
PRODUCT = "Aolynk CB304n Cable网桥"
OPERATION = "单击<重启>按钮，将完成路由器重启功能。"
PARTIAL_ANSWER = "单击“重启”按钮，将完成路由器重启功能。现有资料没有说明重启后何时生效。"
UNSUPPORTED = "重启操作在点击“重启”按钮后将立即执行。"


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(("timing_sentence", "expected"), [
    ("", PARTIAL_ANSWER),
    ("配置更改会在设备重启后立即生效。", "配置更改会在设备重启后立即生效。"),
])
@pytest.mark.parametrize("quote_operation", [True, False])
def test_restart_timing_uses_only_proven_claims(monkeypatch, stream, timing_sentence, expected, quote_operation):
    # 即使模型提交了生效结论，返回内容也必须在本轮证据中逐句核对。
    def chat(*, json_mode=False):
        assert timing_sentence, "没有相关时效句时不用再调用模型"
        content = (
            json.dumps({"operation_quote": OPERATION if quote_operation else "",
                        "effect_quote": "配置更改会在设备重启后立即生效。"}, ensure_ascii=False)
            if json_mode else UNSUPPORTED
        )
        return SimpleNamespace(invoke=lambda _: SimpleNamespace(content=content))

    events = []
    monkeypatch.setattr(answer_service.llm_providers, "chat", chat)
    monkeypatch.setattr(answer_service.llm_providers, "stream_chat_text", lambda _: iter([UNSUPPORTED]))
    monkeypatch.setattr(answer_service, "publish", lambda _session, _kind, body: events.append(body["delta"]))
    state = {
        "original_query": QUESTION,
        "rewritten_query": "Aolynk CB304n 设备重启怎么操作？",
        "business_tag": "product_knowledge",
        "item_names": [PRODUCT],
        "session_id": "s",
        "is_stream": stream,
        "reranked_docs": deal_rrf_and_web_result([
            {"item_name": PRODUCT, "content": "| 单击<重启>按钮，将完成路由器重 启功能。 |\n" + timing_sentence},
            {"item_name": PRODUCT, "content": "ALG 配置立即生效。"},
            {"item_name": "其他网桥", "content": "配置更改会在设备重启后立即生效。"},
        ], []),
    }

    answer_service.call_llm_deal_answer(state, "prompt")

    assert state["answer"] == expected
    assert "".join(events) == (expected if stream else "")


def test_agentic_verifies_the_partial_answer_without_the_unsupported_claim(monkeypatch):
    monkeypatch.setattr(answer_service, "build_history_context", lambda *a, **k: "")
    ref = doc_to_evidence({"chunk_id": "a", "item_name": PRODUCT, "text": OPERATION})
    tools = SimpleNamespace(execute=lambda *a: {"status": "ok", "evidence": [ref]})

    def model(kind, payload, _timeout):
        if kind == "assess":
            # 现场评审误把已有重启说明的题判为缺资料。
            return {"coverage": "missing", "evidence_ids": []}
        if kind == "draft":
            return {"answer": UNSUPPORTED, "operation_quote": OPERATION, "effect_quote": "设置立即生效。"}
        assert kind == "verify"
        assert payload["draft"] == PARTIAL_ANSWER
        return {"supported": True, "evidence_ids": [ref.evidence_id]}

    result = run_agentic({"original_query": QUESTION, "rewritten_query": "怎么重启设备？", "item_names": [PRODUCT]},
                         tools=tools, model=model, persist=False)

    assert result["agentic_verified"] is True
    assert result["answer"] == PARTIAL_ANSWER
