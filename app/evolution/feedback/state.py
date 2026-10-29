"""未解决状态的判定。帮助度评分与解决状态分别保留。"""
from __future__ import annotations


def is_unresolved(row: dict) -> bool:
    if row.get("resolved") is True:
        return False
    if row.get("resolved") is False:
        return True
    if row.get("adopt") is True:
        return False
    thumbs = row.get("thumbs")
    return row.get("adopt") is False or (type(thumbs) in (int, float) and thumbs < 0)


def unresolved_filter() -> dict:
    """与 is_unresolved 一致的 Mongo 条件；兼容旧 adopt 字段。"""
    return {"$or": [
        {"resolved": False},
        {"resolved": {"$nin": [True, False]}, "adopt": {"$ne": True},
         "$or": [{"adopt": False}, {"thumbs": {"$lt": 0}}]},
    ]}
