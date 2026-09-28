"""
配置读取公共工具，统一处理 `.env` 加载与环境变量类型转换。

本模块是全项目**唯一**读取环境变量的入口：只有 `app/shared/config/*` 允许调用
`env_str/env_bool/env_int/env_float`，业务模块一律通过 `settings` 聚合对象取值。
"""
from __future__ import annotations

import os

from dotenv import load_dotenv

# override=True：以 .env 覆盖进程已存在的同名环境变量，保证配置一致性
load_dotenv(override=True)

_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}


def env_str(name: str, default: str = "") -> str:
    """读取字符串配置；变量不存在时返回默认值。"""
    value = os.getenv(name)
    return value if value is not None else default


def env_bool(name: str, default: bool = False) -> bool:
    """读取布尔配置，兼容常见的真假值写法；无法识别时返回默认值。"""
    value = os.getenv(name)
    if value is None or value == "":
        return default

    normalized = value.strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    return default


def env_int(name: str, default: int = 0) -> int:
    """读取整数配置；变量为空或格式非法时返回默认值。"""
    value = os.getenv(name)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError:
        return default


def env_float(name: str, default: float = 0.0) -> float:
    """读取浮点数配置；变量为空或格式非法时返回默认值。"""
    value = os.getenv(name)
    if value is None or value == "":
        return default
    try:
        return float(value)
    except ValueError:
        return default
