"""候选内容 PII 脱敏与拦截测试。"""
from __future__ import annotations

from app.evolution.candidate.pii import contains_pii, redact, sanitize_candidate


def test_redact_replaces_sensitive_patterns():
    text = "联系 13800138000 或 a@b.com，身份证 110101199003071234"
    result = redact(text)
    assert "13800138000" not in result
    assert "a@b.com" not in result
    assert "110101199003071234" not in result


def test_contains_pii_only_for_hard_identifiers():
    assert contains_pii("手机 13900139000")
    # 长数字串（可能是产品型号）只脱敏、不判 PII
    assert not contains_pii("型号 1801234")


def test_sanitize_candidate_returns_flag():
    question, answer, has_pii = sanitize_candidate("如何联系？", "邮箱 support@example.com")
    assert has_pii is True
    assert "support@example.com" not in answer
