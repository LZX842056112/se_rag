"""主体名提取提示词的回归守卫。

回归背景（本轮联调发现）：会话历史在讨论“HAK180”后，用户改问另一个主体
“e2e_ui_20260928b 机型的额定功率是多少？”，模型却把主体替换成历史里的“HAK180”，
导致召回按错误主体过滤 → 答“无法作答”。根因有二：
1) 提示词缺少“当前问题主体优先、禁止用历史主体替换”的强约束；
2) 规则 6 的示例直接用了真实主体名“HAK180”，既污染识别又诱导模型照抄。

以下静态断言锁住修复，避免提示词回退。
"""
from __future__ import annotations

from app.shared.runtime.prompts import load_prompt

PROMPT_NAME = "rewritten_query_and_itemnames"


def test_prompt_renders_placeholders_without_keyerror():
    """JSON 结构示例的花括号必须转义，否则 .format() 会抛 KeyError。"""
    rendered = load_prompt(
        PROMPT_NAME, query="示例问题Q", history_text="示例历史H"
    )
    assert "示例问题Q" in rendered
    assert "示例历史H" in rendered
    assert '"item_names"' in rendered


def test_prompt_forbids_history_subject_override():
    """必须显式声明：当前问题已出现主体时，不得用历史会话主体替换。"""
    rendered = load_prompt(PROMPT_NAME, query="Q", history_text="H")
    assert "以当前问题为准" in rendered
    assert "绝不允许用" in rendered


def test_prompt_examples_do_not_reuse_real_subject_names():
    """示例型号名不得复用库内真实主体名（此前用 HAK180 做示例，直接诱导模型照抄）。"""
    rendered = load_prompt(PROMPT_NAME, query="Q", history_text="H")
    assert "HAK180" not in rendered
    assert "HAK 180" not in rendered
