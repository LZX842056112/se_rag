"""分组与忽略匹配使用同一归一化规则，历史隔离键保持不变。"""
from app.evolution.issue_groups import context_key, group_key


def test_context_key_preserves_question_spacing_and_product_equivalence():
    row = {"_id": "a", "session_id": "s", "query": "  温度   多少？ ",
           "item_names": [" ＨＡＫ １８０ ", "HAK180", ""], "business_tag": "product"}
    expected = ("s", "温度 多少", ("hak180",), "product")
    assert context_key(row) == expected
    assert group_key(row) == (*expected, ("active",))


def test_original_question_is_used_only_when_context_is_complete():
    row = {"_id": "a", "session_id": "s", "original_query": "原始问题", "query": "登记问题",
           "item_names": ["HAK180"], "business_tag": "product"}
    assert context_key(row)[1] == "原始问题"
    row["item_names"] = []
    assert context_key(row) is None
    assert group_key(row) == ("s", "登记问题", ("unknown-product:a",), "product", ("active",))


def test_unknown_product_and_completed_history_are_never_merged():
    row = {"_id": "a", "session_id": "s", "query": "温度多少", "item_names": [], "business_tag": "product"}
    assert group_key(row) != group_key({**row, "_id": "b"})
    row.update(item_names=["HAK180"], status="resolved", completed_at=4)
    assert group_key(row)[-1] == ("resolved", 4)
    assert group_key(row) != group_key({**row, "completed_at": 5})
    assert group_key({**row, "status": "dismissed", "dismissed_by_gap_id": "gap-a"})[-1] == ("dismissed", "gap-a")


def test_session_product_and_business_changes_keep_distinct_contexts():
    row = {"session_id": "s", "query": "温度多少", "item_names": ["HAK180"], "business_tag": "product"}
    for patch in ({"session_id": "other"}, {"item_names": ["HL3040"]}, {"business_tag": "after_sales"}):
        assert context_key(row) != context_key({**row, **patch})
