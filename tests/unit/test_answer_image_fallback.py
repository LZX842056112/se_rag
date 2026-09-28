"""「无法作答」与图片互斥的回归测试（联调实测缺陷 D15）。

背景：说明书类切片常是「零碎文字 + 多张配图」。模型读到零碎文字判为答不出、
输出兜底话术，但 ``extract_text_image_url`` 仍会从同一批切片抽出图片并渲染，
于是出现「文字说答不出、界面却给出图」的自相矛盾。

约定：兜底话术与图片互斥——命中兜底话术时不得回填/展示图片。
"""
from __future__ import annotations

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


def test_normal_answer_still_extracts_images():
    """正常作答时图片照常回填，修复不能误伤正常链路。"""
    state = {"answer": "烫金机的额定功率为 55W。", "reranked_docs": _docs()}
    extract_text_image_url(state)
    assert state["image_urls"] == [IMG_A, IMG_B]


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
