"""前端静态检查：页面保持「纯标记 + 外链资源」，公共能力只有一份实现。

回归背景：
- `formatTime is not defined`：删除 common.js 后 chat 页仍裸调旧全局，刷新历史整段报错；
- 页面内联 CSS/JS 让 HTML 膨胀到 30+ KB 且无法被浏览器缓存，故改为外链 + 内容指纹。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PAGES_DIR = PROJECT_ROOT / "app" / "resources" / "html"
JS_DIR = PROJECT_ROOT / "app" / "resources" / "js"
CSS_DIR = PROJECT_ROOT / "app" / "resources" / "css"

PAGE_ASSETS = {
    "chat.html": ("app.css", "chat.css", "app.js", "chat.js"),
    "approval.html": ("app.css", "approval.css", "app.js", "approval.js"),
    "import.html": ("app.css", "import.css", "app.js", "import.js"),
}

# 页面/脚本里可能以短名使用的公共函数；只要裸调用，就必须别名到 App.*
HELPERS = ("escapeHtml", "formatTime", "formatDateTime", "nowTime", "showToast", "toast", "esc", "fmtTs")
LEGACY_GLOBALS = ("resolveApiBase", "DEFAULT_HEADERS", "formatTs")


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


@pytest.mark.parametrize("page_name", list(PAGE_ASSETS))
def test_page_is_markup_only(page_name: str):
    """页面不得再包含内联 <style> / <script> 代码块，也不得引用已删除的 common.js。"""
    source = (PAGES_DIR / page_name).read_text(encoding="utf-8")
    assert "<style" not in source, f"{page_name} 仍有内联样式，请移到 /static/*.css"
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", source), f"{page_name} 仍有内联脚本"
    assert "/js/common.js" not in source
    assert "<script src=" in source and "app.js" in source


@pytest.mark.parametrize("page_name,assets", PAGE_ASSETS.items())
def test_page_references_versioned_whitelisted_assets(page_name: str, assets: tuple[str, ...]):
    """页面引用的静态资源必须存在，且带上内容指纹参数（否则长缓存会锁死旧版本）。"""
    source = (PAGES_DIR / page_name).read_text(encoding="utf-8")
    refs = re.findall(r'(?:href|src)="/static/([^"?]+)(\?v=\{\{ASSET_V\}\})?"', source)
    assert refs, f"{page_name} 没有引用任何静态资源"
    referenced = [name for name, _ in refs]
    assert sorted(referenced) == sorted(assets), f"{page_name} 资源集合不符：{referenced}"
    for name, version in refs:
        assert version, f"{page_name} 引用的 {name} 缺少 ?v={{ASSET_V}} 版本参数"
        assert (JS_DIR if name.endswith(".js") else CSS_DIR).joinpath(name).exists(), f"{name} 不存在"


@pytest.mark.parametrize("page_name", list(PAGE_ASSETS))
def test_pages_use_shared_helpers_only(page_name: str):
    """页面脚本不得使用旧全局；裸调用的公共函数必须已别名。"""
    page_source = (PAGES_DIR / page_name).read_text(encoding="utf-8")
    script_name = page_name.replace(".html", ".js")
    script_source = (JS_DIR / script_name).read_text(encoding="utf-8")
    for legacy in LEGACY_GLOBALS:
        assert legacy not in page_source and legacy not in script_source, f"{page_name} 仍使用旧全局 {legacy}"
    missing = bare_helpers_without_alias(script_source)
    assert not missing, f"{script_name} 裸调用了未别名的公共函数：{sorted(missing)}"


@pytest.mark.parametrize("page_name", list(PAGE_ASSETS))
def test_pages_do_not_use_native_modals(page_name: str):
    """原生 confirm/alert/prompt 会阻塞渲染进程（联调时卡死标签页），统一用页内浮层。"""
    script_source = (JS_DIR / page_name.replace(".html", ".js")).read_text(encoding="utf-8")
    for modal in ("confirm", "alert", "prompt"):
        assert not re.search(rf"(?<![\w.]){modal}\s*\(", script_source), (
            f"{page_name} 仍在使用原生 {modal}()，请改用 App.{modal if modal == 'confirm' else 'toast'}()"
        )


def test_shared_library_has_no_page_specific_dead_code():
    """公共库只放跨页能力：不应出现仅某页使用的历史遗留函数名。"""
    source = (JS_DIR / "app.js").read_text(encoding="utf-8")
    for legacy in ("shouldShowImagesByAnswer", "parseImagesFromTextLoosely", "resolveApiBase"):
        assert legacy not in source, f"app.js 仍包含历史遗留实现：{legacy}"
    for required in ("renderAnswerWithImages", "openStream", "confirm", "create"):
        assert required in source
