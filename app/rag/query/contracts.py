"""查询与持久化共用的意图类型。"""
from typing import Literal

Intent = Literal["rag", "chitchat"]
HistoryIntent = Literal["", "rag", "chitchat"]
