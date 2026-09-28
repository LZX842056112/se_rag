"""候选质量判定测试：把「无信息」型答案挡在知识库之外。"""
from __future__ import annotations

from app.evolution.quality import looks_like_non_answer


def test_refusal_style_answers_are_flagged():
    """真实事故原文：审批通过后客服依旧答不出，因为「知识」本身是“去问官方”。"""
    sample = ("根据提供的参考片段，未提及 HAK 180 烫金机的蓝牙配对码。该设备支持蓝牙连接，"
              "但具体配对码信息未在文档中说明。建议查阅设备说明书或联系 Brother 官方客户支持获取准确信息。")
    assert looks_like_non_answer(sample) is True


def test_common_non_answer_phrases():
    for text in ("文档中无相关说明。", "未查询到该问题相关信息。", "无法作答。",
                 "资料中无此项内容，建议咨询厂商。", "Insufficient information to answer."):
        assert looks_like_non_answer(text) is True, text


def test_real_answers_pass():
    for text in ("HAK 180 的额定工作温度为 0~40 摄氏度。",
                 "标准包装清单：主机 1 台、电源线 1 根、快速入门指南 1 本。",
                 "配对码默认为 0000，可在设置-蓝牙中修改。"):
        assert looks_like_non_answer(text) is False, text


def test_empty_or_too_short_answers_are_flagged():
    assert looks_like_non_answer("") is True
    assert looks_like_non_answer(None) is True
    assert looks_like_non_answer("见说明书") is True
