"""答案配图必须由当前答案选用，并能在实际生成上下文中核验。"""
from types import SimpleNamespace

import pytest

from app.process.query.agent.state import create_query_default_state
from app.rag.query import answer_service as svc


PANEL = "https://images.local/hak180/panel.jpg"
OK = "https://images.local/hak180/ok.png"
UNKNOWN = "https://images.local/other/unknown.png"
CONTEXT = f"按上下键选择温度。![温度设置面板]({PANEL})\n![OK按钮]({OK})"


@pytest.mark.parametrize('marker', ['【图片】', '[图片]'])
def test_empty_trailing_image_section_does_not_leak_a_marker(marker):
    state = {'answer': 'HL3040不支持无线网络。\n\n' + marker, 'answer_contexts':[CONTEXT]}
    svc.extract_text_image_url(state)
    assert state['answer'] == 'HL3040不支持无线网络。'
    assert state['image_urls'] == []


@pytest.mark.parametrize("answer", [
    "HAK 180 烫金机没有蓝牙。",
    "有五个转印温度设置值，默认值为中间值。",
    "额定功率为55W。",
])
def test_factual_answers_without_selected_images_have_no_gallery(answer):
    state = {"answer": answer, "answer_contexts": [CONTEXT],
             "reranked_docs": [{"text": CONTEXT}], "image_urls": [UNKNOWN]}
    svc.extract_text_image_url(state)
    assert state["image_urls"] == []
    assert state["answer"] == answer


@pytest.mark.parametrize("reference", [f"![温度设置面板]({PANEL})", f"【图片】\n{PANEL}", f"[图片]\n{PANEL}"])
def test_operation_answer_only_keeps_explicitly_selected_context_image(reference):
    state = {"answer": "按上下键选择温度，面板见图。\n" + reference,
             "answer_contexts": [CONTEXT], "reranked_docs": [{"text": CONTEXT}]}
    svc.extract_text_image_url(state)
    assert state["image_urls"] == [PANEL]
    assert PANEL in state["answer"] and OK not in state["answer"]


def test_requested_image_outside_actual_context_is_removed():
    state = {"answer": f"按上下键设置温度。\n![设置图]({UNKNOWN})",
             "answer_contexts": [CONTEXT], "reranked_docs": [{"text": f"![未使用]({UNKNOWN})"}]}
    svc.extract_text_image_url(state)
    assert state["image_urls"] == []
    assert UNKNOWN not in state["answer"]
    assert "按上下键设置温度" in state["answer"]


def test_images_are_deduplicated_in_answer_order():
    state = {"answer": f"先确认，再调温。\n![确认]({OK})\n![面板]({PANEL})\n![面板]({PANEL})",
             "answer_contexts": [CONTEXT]}
    svc.extract_text_image_url(state)
    assert state["image_urls"] == [OK, PANEL]


@pytest.mark.parametrize("answer", [
    "1. 点击【图片】菜单。\n2. 选择文件并确认。",
    "1. 点击[图片]菜单。\n2. 选择文件并确认。",
    "打开菜单：\n【图片】\n然后选择文件并确认。",
])
def test_image_menu_names_and_instruction_text_are_not_truncated(answer):
    state = {"answer": answer, "answer_contexts": [CONTEXT]}
    svc.extract_text_image_url(state)
    assert state["answer"] == answer
    assert state["image_urls"] == []


def test_signed_image_url_is_supported_without_dropping_its_query():
    signed = PANEL.replace(".jpg", ".JPG") + "?signature=abc&expires=123"
    state = {"answer": f"按图设置。\n![面板]({signed})",
             "answer_contexts": [f"![面板]({signed})"]}
    svc.extract_text_image_url(state)
    assert state["image_urls"] == [signed]


@pytest.mark.parametrize("url", ["javascript:alert(1)", "data:image/png;base64,abc", "/panel.jpg"])
def test_non_http_image_references_are_not_published(url):
    state = {"answer": f"按图设置。\n![面板]({url})", "answer_contexts": [f"![面板]({url})"]}
    svc.extract_text_image_url(state)
    assert state["image_urls"] == []
    assert url not in state["answer"]


@pytest.mark.parametrize("blocked", ["无法作答", "现有证据不足，请交由负责人核实。"])
def test_fallback_or_handoff_never_shows_images(blocked):
    state = {"answer": f"{blocked}\n【图片】\n{PANEL}", "answer_contexts": [CONTEXT],
             "handoff_required": "负责人" in blocked}
    svc.extract_text_image_url(state)
    assert state["image_urls"] == []
    assert PANEL not in state["answer"]


@pytest.mark.parametrize("business, expected", [("", [PANEL]), ("pricing", [])])
def test_parent_images_must_be_in_the_actual_prompt(monkeypatch, business, expected):
    monkeypatch.setattr(svc, "build_history_context", lambda *a, **kw: "")
    state = create_query_default_state(
        business_tag=business, answer=f"按图操作。\n![面板]({PANEL})",
        reranked_docs=[{"text": "子块", "parent_content": f"![面板]({PANEL})"}])
    svc.load_answer_prompt(state)
    svc.extract_text_image_url(state)
    assert state["image_urls"] == expected


def test_document_image_url_not_sent_to_generation_is_not_displayed():
    state = {"answer": f"设置说明。\n【图片】\n{PANEL}", "answer_contexts": ["纯文字证据"],
             "reranked_docs": [{"text": "纯文字证据", "url": PANEL}]}
    svc.extract_text_image_url(state)
    assert state["image_urls"] == []
    assert PANEL not in state["answer"]


def test_prefilled_answer_clears_images_from_previous_turn(monkeypatch):
    monkeypatch.setattr(svc, "apply_business_policy", lambda state: None)
    monkeypatch.setattr(svc, "backfill_evolution_outputs", lambda state: state)
    monkeypatch.setattr(svc, "save_answer_message_history", lambda state: None)
    state = create_query_default_state(answer="请先选择产品", image_urls=[PANEL])
    svc.generate_answer(state)
    assert state["image_urls"] == []


@pytest.mark.parametrize("stream", [False, True])
def test_selected_images_are_persisted_after_generation_with_one_model_call(monkeypatch, stream):
    saved, calls = [], []
    answer = f"按上下键设置温度，面板见图。\n![温度设置面板]({PANEL})"
    class Model:
        def invoke(self, messages):
            calls.append(messages)
            return SimpleNamespace(content=answer)
        def stream(self, messages):
            calls.append(messages)
            yield SimpleNamespace(content=answer[:8])
            yield SimpleNamespace(content=answer[8:])
    monkeypatch.setattr(svc.llm_providers, "chat", lambda: Model())
    monkeypatch.setattr(svc, "build_history_context", lambda *a, **kw: "")
    monkeypatch.setattr(svc, "publish", lambda *a: None)
    monkeypatch.setattr(svc.settings.evolution, "enabled", False)
    monkeypatch.setattr(svc.history_repository, "save_message", lambda **kw: saved.append(kw) or "a1")
    state = create_query_default_state(is_stream=stream, original_query="温度怎么设置", rewritten_query="温度怎么设置",
        reranked_docs=[{"chunk_id": "c1", "text": CONTEXT}])
    svc.generate_answer(state)
    assert len(calls) == 1 and CONTEXT in calls[0][0].content
    assert state["image_urls"] == saved[0]["image_urls"] == [PANEL]
    assert saved[0]["text"] == state["answer"] and OK not in state["answer"]


@pytest.mark.parametrize("persist", [False, True])
def test_agentic_answer_uses_same_image_validation_before_persistence_or_streaming(monkeypatch, persist):
    from app.rag.agentic.controller import run_agentic
    from app.rag.agentic.evidence import doc_to_evidence
    from app.shared.utils import sse_broker
    ref = doc_to_evidence({"chunk_id": "c1", "item_name": "HAK180", "text": CONTEXT,
                           "document_version": "v1"})
    saved, emitted, stages = [], [], []
    class Tools:
        def execute(self, *args):
            return {"status": "ok", "evidence": [ref.model_dump()]}
    def model(stage, payload, timeout):
        stages.append(stage)
        if stage == "assess":
            return {"coverage": "supported", "evidence_ids": [ref.evidence_id]}
        if stage == "draft":
            return {"answer": f"按上下键设置温度，面板见图。\n![面板]({PANEL})"}
        if stage == "verify":
            return {"supported": True, "evidence_ids": [ref.evidence_id]}
        raise AssertionError(stage)
    monkeypatch.setattr(svc, "build_history_context", lambda *a, **kw: "")
    monkeypatch.setattr(svc.history_repository, "save_message", lambda **kw: saved.append(kw) or "a1")
    monkeypatch.setattr(sse_broker, "publish", lambda sid, event, data: emitted.append(data))
    state = create_query_default_state(is_stream=True, original_query="温度怎么设置", rewritten_query="温度怎么设置",
                                       item_names=["HAK180"])
    result = run_agentic(state, tools=Tools(), model=model, persist=persist)
    assert result["image_urls"] == [PANEL]
    assert stages == ["assess", "draft", "verify"]
    assert OK not in result["answer"]
    assert len(saved) == len(emitted) == int(persist)
    if persist:
        assert saved[0]["image_urls"] == [PANEL]
        assert emitted[0]["delta"] == saved[0]["text"] == result["answer"]
