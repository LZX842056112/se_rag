"""Milvus 过滤表达式构造与转义测试（防注入 / 防解析失败）。"""
from __future__ import annotations

from app.shared.clients.milvus_gateway import eq_expr, escape_milvus_string, in_expr


def test_escape_handles_quotes_backslash_and_newline():
    assert escape_milvus_string("a'b") == "a\\'b"
    assert escape_milvus_string("a\\b") == "a\\\\b"
    assert escape_milvus_string("a\nb") == "a b"
    assert escape_milvus_string(None) == ""


def test_eq_and_in_expr_are_escaped_and_quoted():
    assert eq_expr("file_title", "hak180") == "file_title == 'hak180'"
    expr = in_expr("item_name", ["HAK 180", "a'b"])
    assert expr == "item_name in ['HAK 180', 'a\\'b']"
    # 不能出现未转义的 Python repr（旧实现会产生裸列表字面量）
    assert "[\"HAK 180\"" not in expr


def test_in_expr_with_empty_list_is_still_valid_shape():
    assert in_expr("item_name", []) == "item_name in []"
