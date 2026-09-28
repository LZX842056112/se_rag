"""
LLM 返回 JSON 的统一解析工具。

各业务模块此前各自实现「剥掉 ```json 代码围栏 + json.loads」：
``item_name_confirm_service``、``evolution/candidate/generator``、
``evolution/online_eval/grounding``。此处收敛为一份实现。
"""
from __future__ import annotations

import json
from typing import Any


def strip_code_fence(text: str) -> str:
    """剥掉 ``` / ```json 代码围栏，返回纯文本。"""
    cleaned = str(text or "").strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1].rsplit("```", 1)[0]
    return cleaned.strip()


def parse_json_object(text: str) -> dict[str, Any]:
    """把 LLM 返回文本解析为 dict；非对象或非法 JSON 时抛 ``ValueError``。"""
    parsed = json.loads(strip_code_fence(text))
    if not isinstance(parsed, dict):
        raise ValueError(f"期望 JSON 对象，实际为 {type(parsed).__name__}")
    return parsed
