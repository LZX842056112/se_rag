"""
项目路径解析：全项目 ``PROJECT_ROOT`` 的唯一来源。

优先读环境变量 ``PROJECT_ROOT``（生产可用），否则从本文件所在目录逐级向上
查找标识文件（默认 ``.env``），找到即返回该目录。
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv


def get_project_root(identifier: str = ".env") -> Path:
    """返回项目根目录；找不到标识文件时抛 ``FileNotFoundError``。"""
    env_root = os.getenv("PROJECT_ROOT")
    if env_root and Path(env_root).absolute().exists():
        return Path(env_root).absolute()

    current_dir = Path(__file__).absolute().parent
    while True:
        if (current_dir / identifier).exists():
            load_dotenv(dotenv_path=current_dir / identifier)
            return current_dir
        if current_dir == current_dir.parent:
            break
        current_dir = current_dir.parent

    raise FileNotFoundError(f"未找到项目根目录标识「{identifier}」，且环境变量 PROJECT_ROOT 未配置")


PROJECT_ROOT = get_project_root(".env")
