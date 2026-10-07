"""导入链路的输入素材读取（chunks 与主体名）。

历史问题：``item_name_service`` 与 ``embedding_service`` 各自实现了一份「chunks 为空则
从 md_path 同名的 json 备份读取」+「主体名缺省回退」的样板代码，逻辑逐行相同。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.shared.runtime.logger import logger
from app.shared.utils.require import fail, require_str


def load_chunks(state: dict) -> list[dict[str, Any]]:
    """读取待处理切片：优先取 state，其次从 ``<md_path 同名>.json`` 备份恢复。

    走备份回退时显式告警：``split_document`` 已保证「切分为空即失败」，能走到这里说明是
    刻意跳过切分的调用方（如评估链路）在读取备份——若是异常导致的空产出，回退会把上一次
    导入的**陈旧切片**当成本次结果写回知识库。
    """
    chunks = state.get("chunks")
    if chunks:
        return chunks

    md_path = state.get("md_path")
    if md_path:
        backup = Path(md_path).with_name(f"{Path(md_path).stem}.json")
        if backup.exists():
            chunks = json.loads(backup.read_text(encoding="utf-8"))
            if chunks:
                logger.warning(f"chunks 为空，已回退读取本地切片备份（可能是陈旧数据）：{backup}")
                return chunks
    fail("chunks", "为空且本地备份文件不可用")


def load_subject(state: dict, key: str, *, default_name: str = "default_item_name") -> str:
    """读取主体名（``item_name`` / ``file_title``），缺失时依次回退 md 文件名与默认值。"""
    value = state.get(key)
    if value:
        return str(value)
    md_path = state.get("md_path")
    fallback = Path(md_path).stem if md_path and Path(md_path).exists() else default_name
    return require_str(None, key, default=fallback)
