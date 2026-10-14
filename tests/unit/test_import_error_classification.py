"""导入错误分类回归：失败原因必须落成稳定的机器码。

回归背景：历史实现把所有异常统一归为 failed，前端与排障无法区分失败原因。
"""
from __future__ import annotations

import pytest

from app.rag.import_.errors import RETRYABLE_CODES, classify_error, is_retryable


@pytest.mark.parametrize(
    "message,expected",
    [
        ("MinerU 解析超时（600s），batch_id=b1", "mineru_timeout"),
        ("MinerU 解析任务失败：batch_id=b1", "mineru_failed"),
        ("解析结果解压后没有 Markdown 文件：http://x/z.zip", "parse_failed"),
        ("chunks切分后为空，业务无法继续进行，提前终止！", "split_empty"),
        ("insert_count对账失败：写入 3 条，期望 5 条，业务无法继续进行，提前终止！", "index_count_mismatch"),
        ("Milvus 客户端不可用", "storage_unavailable"),
        ("不支持的文档类型（仅支持 md / pdf）", "unsupported_type"),
    ],
)
def test_keyword_classification(message, expected):
    assert classify_error(RuntimeError(message))[0] == expected


def test_timeout_is_classified_by_type():
    """超时按异常类型判定：内建超时不带 MinerU 关键字也要能识别。"""
    assert classify_error(TimeoutError("timed out"))[0] == "mineru_timeout"


def test_unknown_error_falls_back_to_internal():
    code, message = classify_error(RuntimeError("完全没见过的错误"))
    assert code == "internal_error"
    assert message == "完全没见过的错误"


@pytest.mark.parametrize("code", ["mineru_timeout", "mineru_failed", "storage_unavailable", "internal_error"])
def test_retryable_codes(code):
    assert is_retryable(code)


@pytest.mark.parametrize("code", ["split_empty", "unsupported_type", "index_count_mismatch"])
def test_deterministic_codes_are_not_retryable(code):
    """确定性错误不得重试：重跑只会重复失败并浪费云端配额。"""
    assert code not in RETRYABLE_CODES
    assert not is_retryable(code)
