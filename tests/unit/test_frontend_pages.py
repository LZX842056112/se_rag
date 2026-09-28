"""前端页面静态检查：三页引用的公共库能力必须真的存在。

回归背景：`/js/common.js` 被 `/static/app.js` 取代后，chat.html 仍在调用旧全局
`formatTime(ts)`，导致「刷新后历史回显」整段抛 `ReferenceError: formatTime is not defined`，
并被 catch 静默吞成一句 toast（浏览器联调时才暴露）。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PAGES_DIR = PROJECT_ROOT / "app" / "resources" / "html"

LEGACY_GLOBALS = ("resolveApiBase", "DEFAULT_HEADERS", "formatTs")

# 页面里可能以短名使用的公共函数；只要裸调用，就必须在页内别名到 App.*
HELPERS = ("escapeHtml", "formatTime", "formatDateTime", "nowTime", "showToast", "toast", "esc", "fmtTs")


def bare_helpers_without_alias(source: str) -> set[str]:
    """返回「被裸调用但没有别名到 App.*」的公共函数名集合。"""
    aliased = set(re.findall(r"const\s+(\w+)\s*=\s*App\.\w+", source))
    aliased |= set(re.findall(r"const\s+(\w+)\s*=\s*\([^)]*\)\s*=>\s*App\.\w+", source))
    missing: set[str] = set()
    for helper in HELPERS:
        if re.search(rf"(?<![\w.]){re.escape(helper)}\s*\(", source) and helper not in aliased:
            missing.add(helper)
    return missing


def test_guard_detects_the_original_history_regression():
    """守卫自检：原始缺陷（裸用 formatTime 且未别名）必须被识别出来。"""
    buggy_snippet = """
    const escapeHtml = App.escapeHtml;
    function addUserMsgWithTime(text, ts){
      const html = `<div>${escapeHtml(text)}</div><div>${formatTime(ts)}</div>`;
      return html;
    }
    """
    assert bare_helpers_without_alias(buggy_snippet) == {"formatTime"}
    fixed_snippet = buggy_snippet + "\n const formatTime = App.formatTime;\n"
    assert bare_helpers_without_alias(fixed_snippet) == set()


@pytest.mark.parametrize("page_name", ["chat.html", "approval.html", "import.html"])
def test_pages_use_shared_assets_only(page_name: str):
    source = (PAGES_DIR / page_name).read_text(encoding="utf-8")
    assert "/js/common.js" not in source, "页面仍引用已删除的 common.js"
    assert "/static/app.js" in source, "页面未引用公共库 app.js"
    assert "/static/app.css" in source, "页面未引用公共样式 app.css"
    for legacy in LEGACY_GLOBALS:
        assert legacy not in source, f"页面仍使用旧全局 {legacy}"


@pytest.mark.parametrize("page_name", ["chat.html", "approval.html", "import.html"])
def test_shared_helpers_are_aliased(page_name: str):
    source = (PAGES_DIR / page_name).read_text(encoding="utf-8")
    missing = bare_helpers_without_alias(source)
    assert not missing, f"{page_name} 裸调用了未别名的公共函数：{sorted(missing)}"


@pytest.mark.parametrize("page_name", ["chat.html", "approval.html", "import.html"])
def test_pages_do_not_use_native_modals(page_name: str):
    """原生 confirm/alert/prompt 会阻塞渲染进程（联调时卡死标签页），统一用页内浮层。"""
    source = (PAGES_DIR / page_name).read_text(encoding="utf-8")
    for modal in ("confirm", "alert", "prompt"):
        assert not re.search(rf"(?<![\w.]){modal}\s*\(", source), (
            f"{page_name} 仍在使用原生 {modal}()，请改用 App.{modal if modal == 'confirm' else 'toast'}()"
        )
