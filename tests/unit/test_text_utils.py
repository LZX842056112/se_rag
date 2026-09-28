"""文本归一与主体名等价判定测试。"""
from __future__ import annotations

from app.shared.utils.text import (
    is_same_entity,
    join_subjects,
    name_equivalent,
    name_tokens,
    normalize_item_name,
    split_subjects,
    token_prefix_match,
)

GLOBAL = "default_item_name"


def test_normalize_removes_space_and_case():
    assert normalize_item_name(" HAK 180 烫金机 ") == "hak180烫金机"
    assert normalize_item_name("ＨＡＫ１８０") == "hak180"  # 全角转半角
    assert normalize_item_name(None) == ""


def test_is_same_entity_suffix_rule():
    assert is_same_entity("Brother HAK 180烫金机", "HAK 180 烫金机")
    assert is_same_entity("HAK 180 烫金机", "HAK 180 烫金机")
    # 父/子型号必须视为不同实体（保留反问契约）
    assert not is_same_entity("HAK 180", "HAK 180 烫金机")
    # 尾号不同不能合并
    assert not is_same_entity("HAK 180", "HAK 1800")


def test_token_prefix_match():
    assert token_prefix_match(name_tokens("HAK 180"), name_tokens("HAK 180 烫金机"))
    assert not token_prefix_match(name_tokens("HAK 180"), name_tokens("HAK 1800"))
    assert not token_prefix_match([], name_tokens("HAK 180"))


def test_split_and_join_subjects():
    assert split_subjects("A,B，C,") == ["A", "B", "C"]
    assert join_subjects(["A", "A", " B ", ""]) == "A,B"


def test_name_equivalent_handles_multi_subject_and_global():
    # 未打标签的全局条目始终可召回
    assert name_equivalent(GLOBAL, ["任意主体"], global_item=GLOBAL)
    # 多主体（逗号连接）必须逐主体判断，避免跨主体 token 误判
    assert name_equivalent("HAK 180 烫金机,Brother HAK 180烫金机", ["HAK 180"], global_item=GLOBAL)
    # 不同商品不能互相召回
    assert not name_equivalent("HAK 179 烫金机", ["HAK 180"], global_item=GLOBAL)
