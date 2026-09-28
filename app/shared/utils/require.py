"""
状态 / 参数校验工具：消除各业务模块重复的 ``_require_xxx`` 样板。

约定：校验失败时记录 error 日志并抛 ``ValueError``，消息中包含缺失字段名与
「业务无法继续进行，提前终止」的中文说明，保持与历史行为一致。
"""
from __future__ import annotations

from typing import Any, NoReturn

from app.shared.runtime.logger import logger


def fail(field: str, detail: str = "为空") -> NoReturn:
    """统一校验失败出口：error 日志 + ``ValueError``。"""
    message = f"{field}{detail}，业务无法继续进行，提前终止！"
    logger.error(message)
    raise ValueError(message)


def require_str(value: Any, field: str, default: str | None = None) -> str:
    """要求非空字符串；``value`` 为空时可回退 ``default``，仍为空则失败。"""
    if isinstance(value, str):
        text = value.strip()
        if text:
            return text
    if default is not None and str(default).strip():
        logger.warning(f"{field}为空，使用默认值：{default}")
        return str(default)
    fail(field)


def require_state_str(state: Any, key: str, default: str | None = None) -> str:
    """从 state 中取非空字符串字段。"""
    return require_str(state.get(key) if hasattr(state, "get") else None, key, default)


def require_list(
    value: Any,
    field: str,
    *,
    allow_empty: bool = False,
    default: list | None = None,
) -> list:
    """要求列表字段；``allow_empty=False`` 时空列表视为失败。"""
    if isinstance(value, list) and value:
        return value
    if isinstance(value, list) and allow_empty:
        return value
    if default is not None:
        logger.warning(f"{field}为空，使用默认值")
        return list(default)
    fail(field, "为空" if not isinstance(value, list) else "列表为空")


def require_state_list(state: Any, key: str, *, allow_empty: bool = False, default: list | None = None) -> list:
    """从 state 中取列表字段。"""
    value = state.get(key) if hasattr(state, "get") else None
    return require_list(value, key, allow_empty=allow_empty, default=default)
