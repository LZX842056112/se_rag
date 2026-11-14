"""「无法作答」与图片互斥的回归测试（联调实测缺陷 D15）。

背景：说明书类切片常是「零碎文字 + 多张配图」。模型读到零碎文字判为答不出、
输出兜底话术，但 ``extract_text_image_url`` 仍会从同一批切片抽出图片并渲染，
于是出现「文字说答不出、界面却给出图」的自相矛盾。

约定：兜底话术与图片互斥——命中兜底话术时不得回填/展示图片。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.rag.query.answer_service import extract_text_image_url
from app.shared.utils.answer import is_no_answer

NO_ANSWER = "现有参考内容与历史对话中未查询到该问题相关信息，无法作答"
IMG_A = "http://minio.local/upload-images/hak180使用说明书/a.jpg"
IMG_B = "http://minio.local/upload-images/hak180使用说明书/b.png"


def _docs() -> list[dict]:
    """模拟「零碎文字 + 配图」的说明书切片。"""
    return [
        {"chunk_id": "1", "type": "chunk", "text": f"## 重要事项\nb 安装进纸托板。\n\n![安装示意图]({IMG_A})"},
        {"chunk_id": "2", "type": "chunk", "text": f"打开前盖。\n![装入烫金膜盒]({IMG_B})"},
    ]


def test_no_answer_suppresses_retrieved_images():
    """命中兜底话术时不得回填图片（否则自相矛盾）。"""
    state = {"answer": NO_ANSWER, "reranked_docs": _docs()}
    extract_text_image_url(state)
    assert state["image_urls"] == []


def test_text_only_answer_does_not_attach_unrelated_retrieved_images():
    """功率结论不需要安装配图，不能把检索图片自动附上。"""
    state = {"answer": "烫金机的额定功率为 55W。", "reranked_docs": _docs()}
    extract_text_image_url(state)
    assert state["image_urls"] == []


def test_image_url_field_is_suppressed_too():
    """图片型 url 字段同样受兜底话术抑制。"""
    state = {
        "answer": NO_ANSWER,
        "reranked_docs": [{"chunk_id": "3", "type": "chunk", "url": IMG_A, "text": "说明"}],
    }
    extract_text_image_url(state)
    assert state["image_urls"] == []


def test_is_no_answer_markers():
    """兜底话术判定的两种措辞都要命中，正常答案不得误判。"""
    assert is_no_answer(NO_ANSWER)
    assert is_no_answer("抱歉，无法作答。")
    assert not is_no_answer("该机型保修期为 12 个月。")
    assert not is_no_answer(None)


@pytest.mark.parametrize('is_stream', [False, True])
@pytest.mark.parametrize(('chunks', 'expected'), [
    (['打印服务器支持 Telnet 命令行配置。', '\n\n现有参考内容与', '历史对话中未查询到该问题相关信息，无法', '作答'],
     '打印服务器支持 Telnet 命令行配置。'),
    (['该型号', '不支持彩色打印。'], '该型号不支持彩色打印。'),
    (['现有参考内容与', '历史对话中未查询到该问题相关信息，无法作答'], NO_ANSWER + '。请交由负责人核实。'),
    (['**Answer:**\n', NO_ANSWER], '**Answer:**\n' + NO_ANSWER + '。请交由负责人核实。'),
    (['### 结论\n', NO_ANSWER], '### 结论\n' + NO_ANSWER + '。请交由负责人核实。'),
    (['抱', '歉，现有参考内容与', '历史对话中未查询到该问题相关信息，无法作答。'],
     '抱歉，' + NO_ANSWER + '。请交由负责人核实。'),
])
def test_model_answer_does_not_emit_a_fixed_refusal_after_an_answer(monkeypatch, chunks, expected, is_stream):
    from app.rag.query import answer_service as answers

    events = []
    monkeypatch.setattr(answers, 'publish', lambda session, kind, payload: events.append(payload['delta']))
    monkeypatch.setattr(answers.llm_providers, 'chat', lambda: SimpleNamespace(
        invoke=lambda _: SimpleNamespace(content=''.join(chunks)),
        stream=lambda _: (SimpleNamespace(content=chunk) for chunk in chunks),
    ))
    state = {'is_stream': is_stream, 'session_id': 's'}
    answers.call_llm_deal_answer(state, 'prompt')
    assert state['answer'].strip() == expected
    assert (''.join(events).strip() if is_stream else '') == (expected if is_stream else '')


def test_legacy_fixed_refusal_is_reported_as_a_handoff(monkeypatch):
    from app.rag.query import answer_service as answers
    from app.rag.query.eval_evidence import build_evaluation

    monkeypatch.setattr(answers, 'build_history_context', lambda *a, **k: '')
    monkeypatch.setattr(answers, 'save_answer_message_history', lambda state: None)
    monkeypatch.setattr(answers.llm_providers, 'chat', lambda: SimpleNamespace(
        invoke=lambda _: SimpleNamespace(content=NO_ANSWER),
    ))
    result = answers.generate_answer({'query_mode': 'legacy', 'rewritten_query': '设备支持该功能吗？'})
    assert result['handoff_required'] is True
    assert build_evaluation(result) == {'query_mode': 'legacy', 'outcome': 'handoff', 'contexts': []}


def test_partial_answer_is_not_reported_as_a_handoff(monkeypatch):
    from app.rag.query import answer_service as answers
    from app.rag.query.eval_evidence import build_evaluation

    answer = "VLAN 按逻辑划分网络。手册没有设备配置步骤，这部分无法作答。"
    monkeypatch.setattr(answers, 'build_history_context', lambda *args, **kwargs: '')
    monkeypatch.setattr(answers, 'save_answer_message_history', lambda state: None)
    monkeypatch.setattr(answers, 'compute_groundedness', lambda *args: 1.0)
    monkeypatch.setattr(answers.llm_providers, 'chat', lambda: SimpleNamespace(
        invoke=lambda _: SimpleNamespace(content=answer),
    ))
    state = {'query_mode': 'legacy', 'rewritten_query': '什么是 VLAN？', 'reranked_docs': _docs()}
    result = answers.generate_answer(state)
    assert result['answer'] == answer
    assert build_evaluation(result)['outcome'] == 'answered'
