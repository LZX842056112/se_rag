"""提示词加载：从 ``app/resources/prompts`` 读取模板并渲染占位符。"""
from __future__ import annotations

from functools import lru_cache

from app.shared.utils.paths import PROJECT_ROOT


@lru_cache(maxsize=64)
def _read_prompt_raw(name: str) -> str:
    """读取并缓存未渲染的提示词文本（文件内容不变，可安全缓存）。"""
    prompt_path = PROJECT_ROOT / "app" / "resources" / "prompts" / f"{name}.prompt"
    if not prompt_path.exists():
        raise FileNotFoundError(f"提示词文件不存在：{prompt_path.absolute()}")
    return prompt_path.read_text(encoding="utf-8")


def load_prompt(name: str, **kwargs) -> str:
    """加载提示词并按 ``{占位符}`` 渲染变量。

    :param name: 提示词文件名（不含 ``.prompt`` 后缀）
    :param kwargs: 需要渲染的变量键值对
    """
    raw_prompt = _read_prompt_raw(name)
    if not kwargs:
        return raw_prompt
    return raw_prompt.format(**kwargs)
