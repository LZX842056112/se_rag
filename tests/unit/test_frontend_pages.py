"""前端静态检查：页面保持「纯标记 + 外链资源」，公共能力只有一份实现。

回归背景：
- `formatTime is not defined`：删除 common.js 后 chat 页仍裸调旧全局，刷新历史整段报错；
- 页面内联 CSS/JS 让 HTML 膨胀到 30+ KB 且无法被浏览器缓存，故改为外链 + 内容指纹。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PAGES_DIR = PROJECT_ROOT / "app" / "resources" / "html"
JS_DIR = PROJECT_ROOT / "app" / "resources" / "js"
CSS_DIR = PROJECT_ROOT / "app" / "resources" / "css"
PROMPT_DIR = PROJECT_ROOT / "app" / "resources" / "prompts"

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
    assert 'style="' not in source, f"{page_name} 仍有行内 style 属性，请改为样式表里的 class"
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", source), f"{page_name} 仍有内联脚本"
    assert "/js/common.js" not in source
    assert "<script src=" in source and "app.js" in source


@pytest.mark.parametrize("script_name", sorted(p.name for p in JS_DIR.glob("*.js")))
def test_scripts_generate_no_inline_style_attributes(script_name: str):
    """脚本拼出的 DOM 也不得带行内 style 属性（运行时 `el.style.x=` 不受限于此）。"""
    source = (JS_DIR / script_name).read_text(encoding="utf-8")
    assert 'style="' not in source, f"{script_name} 生成的标记里仍有行内 style 属性"


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


def test_chat_marks_web_citations_and_keeps_them_out_of_feedback():
    """联网引用要标「联网」并给出原网页，但不能进 cited_chunk_ids（URL 不是知识库主键）。"""
    source = (JS_DIR / "chat.js").read_text(encoding="utf-8")
    assert "source === 'web'" in source, "未按来源区分引用标签"
    assert "'联网'" in source, "缺少「联网」标签"
    assert "c.source !== 'web'" in source, "反馈载荷必须剔除联网引用，否则 URL 会被当成 chunk_id"
    assert "未评估" in source, "接地性缺失时应显示「未评估」，不能渲染成 0%"
    assert "safeHttpUrl" in source, "联网链接必须限制为 http/https"


def test_approval_page_exposes_loop_status():
    """闭环状态要能在审批页直接看到（排障时不必翻日志）。"""
    page = (PAGES_DIR / "approval.html").read_text(encoding="utf-8")
    assert 'id="evoStatus"' in page
    script = (JS_DIR / "approval.js").read_text(encoding="utf-8")
    assert "PATHS.evolutionStatus" in script
    assert "evolutionStatus: '/api/evolution/status'" in (JS_DIR / "app.js").read_text(encoding="utf-8")


def test_shared_library_warns_when_page_is_stale():
    """长开的旧页面不会自动换脚本：公共库要能发现「服务端资源版本变了」并提示刷新。"""
    source = (JS_DIR / "app.js").read_text(encoding="utf-8")
    assert "LOADED_ASSET_V" in source, "未记录本页加载的资源版本"
    assert "checkFreshness" in source, "未实现版本比对"
    assert "asset_version" in source, "未读取健康检查里的资源版本"
    assert "Ctrl+F5" in source, "未提示用户刷新"


# ==================== 点选主体后的问句拼装 ====================
# 回归背景：旧实现用「原问题 <= 12 字符」判断「用户只是在指这个主体」，于是
# “hak180怎么安装”（10 字符）被判成纯主体名，意图词「怎么安装」被丢弃，
# 点选后只发出 “Brother HAK 180 烫金机”。主体消解拿到的是一句没有意图的问句，
# 只能顺着历史对话补成上一个话题（如“蓝牙”），表现为「点选主体后回答的是上一个问题」。

def _load_option_question():
    """从 chat.js 中提取 isSubjectOnly / optionQuestion / optionIntentHint 源码并返回。

    前端没有构建步骤也没有测试运行时，这里只把这三个纯函数抠出来跑，
    既不需要 DOM，也不必为测试引入新的依赖。
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("未安装 node，跳过前端函数行为验证")
    source = (JS_DIR / "chat.js").read_text(encoding="utf-8")
    parts: list[str] = []
    for fn_name in ("isSubjectOnly", "optionQuestion", "optionIntentHint"):
        match = re.search(
            rf"^  function {fn_name}\([\s\S]*?^  \}}$", source, flags=re.MULTILINE
        )
        assert match, f"chat.js 中未找到 {fn_name}，函数可能被重命名或删除"
        parts.append(match.group(0))
    return node, "\n".join(parts)


def _run_js_fn(node: str, fn_source: str, fn_name: str, *args) -> str:
    """在 node 里跑抠出来的纯函数。``args`` 按 JSON 序列化，故 bool / list / dict 也可传。

    返回值统一 ``String(...)``：``process.stdout.write`` 不接受布尔值，
    而这里要验证的函数有的返回 bool。
    """
    script = (
        fn_source
        + f"\nprocess.stdout.write(String({fn_name}("
        + ", ".join(json.dumps(a) for a in args) + ")));"
    )
    result = subprocess.run(
        [node, "-e", script], capture_output=True, text=True, timeout=30, check=True
    )
    return result.stdout


def _run_option_question(node: str, fn_source: str, name: str, asked: str) -> str:
    return _run_js_fn(node, fn_source, "optionQuestion", name, asked)


@pytest.mark.parametrize(
    ("asked", "expected"),
    [
        # 关键回归：意图词必须保留，不能被「长度 <= 12」吞掉
        ("hak180怎么安装", "Brother HAK 180 烫金机 hak180怎么安装"),
        ("hak180烫金机怎么换膜", "Brother HAK 180 烫金机 hak180烫金机怎么换膜"),
        # 真的只是在指主体时，不应重复拼接
        ("hak180", "Brother HAK 180 烫金机"),
        ("haK180", "Brother HAK 180 烫金机"),
        ("hak-180", "Brother HAK 180 烫金机"),
        ("Brother HAK 180 烫金机", "Brother HAK 180 烫金机"),
        # 没给问题时退回主体名
        ("", "Brother HAK 180 烫金机"),
    ],
)
def test_option_click_keeps_original_intent(asked: str, expected: str):
    """点选主体后，问句必须保留原问题的**意图**，只补上缺失的主体名。"""
    node, fn_source = _load_option_question()
    assert _run_option_question(node, fn_source, "Brother HAK 180 烫金机", asked) == expected


def test_option_question_does_not_use_length_heuristic():
    """守卫：不得再用「长度 <= N」当判据——正是它当初吞掉了用户的意图词。"""
    source = (JS_DIR / "chat.js").read_text(encoding="utf-8")
    body = re.search(r"function optionQuestion[\s\S]*?\n  \}", source)
    assert body, "未找到 optionQuestion"
    assert not re.search(r"\.length\s*<=", body.group(0)), (
        "optionQuestion 又出现了按长度判断的启发式，会再次丢弃用户的意图词"
    )


def test_rewrite_prompt_forbids_borrowing_intent_from_history():
    """主体消解提示词必须禁止把【历史】的话题当成【当前问题】的意图。"""
    prompt = PROMPT_DIR / "rewritten_query_and_itemnames.prompt"
    text = prompt.read_text(encoding="utf-8")
    assert "意图以当前问题为准" in text, "缺少「意图以当前问题为准」的最高优先级规则"
    assert "不得" in text and "编造" in text, "未明确禁止在问句无意图时编造历史话题"


# ==================== 点选时向服务端直传意图 ====================
# 仅把意图词拼进问句仍不够可靠：实测「纯主体名 + 历史在聊蓝牙」会被模型改写成蓝牙问题。
# 故前端额外把意图原句放进独立的 rewrite_hint 字段，服务端据此确定性拼装。

@pytest.mark.parametrize(
    ("asked", "expected"),
    [
        # 有意图 → 原句要带给服务端
        ("hak180怎么安装", "hak180怎么安装"),
        ("hak180烫金机怎么换膜", "hak180烫金机怎么换膜"),
        ("烫金机如何保养", "烫金机如何保养"),
        # 只是在指主体（无意图可补）→ 不带 hint，服务端退回主体名
        ("hak180", ""),
        ("haK-180", ""),
        ("Brother HAK 180 烫金机", ""),
        ("", ""),
    ],
)
def test_option_intent_hint_carries_only_real_intent(asked: str, expected: str):
    """rewrite_hint 只装「真正的意图」，纯主体名不得当作意图传下去。"""
    node, fn_source = _load_option_question()
    got = _run_js_fn(node, fn_source, "optionIntentHint", "Brother HAK 180 烫金机", asked)
    assert got == expected


def test_submit_query_sends_rewrite_hint_when_present():
    """点选后提交的请求体必须带上 rewrite_hint；手动输入时不带该字段。"""
    source = (JS_DIR / "chat.js").read_text(encoding="utf-8")
    assert "rewrite_hint" in source, "submitQuery 未把意图原句传给服务端"
    assert re.search(r"if\s*\(rewriteHint\)\s*payload\.rewrite_hint\s*=", source), (
        "rewrite_hint 应仅在点选时附带，手动输入不该出现该字段"
    )
    # askSubject 必须把意图透传给 onSend
    ask = re.search(r"function askSubject\([\s\S]*?\n  \}", source)
    assert ask, "未找到 askSubject"
    assert "optionIntentHint" in ask.group(0), "askSubject 未把意图提示传给 onSend"


# ==================== click 事件不得污染 rewrite_hint ====================
# 回归背景：sendBtn 直接绑 onSend 时，浏览器把 MouseEvent 作为第一个实参传入。
# 事件对象序列化为 {}，服务端 Pydantic 校验报
# “rewrite_hint: Input should be a valid string”，表现为「点发送按钮必失败」，
# 而按 Enter 提交正常（Enter 路径显式调用 onSend()）。

def test_send_button_click_does_not_pass_event_as_hint():
    """守卫：onSend 不得被直接注册为 click 监听器。"""
    source = (JS_DIR / "chat.js").read_text(encoding="utf-8")
    assert not re.search(r"addEventListener\(\s*'click'\s*,\s*onSend\s*\)", source), (
        "onSend 被直接绑定为 click 监听器，MouseEvent 会被当成 rewrite_hint 发给服务端"
    )


def test_on_send_discards_non_string_hint():
    """守卫：onSend 必须过滤非字符串入参，非字符串一律按「无 hint」处理。"""
    source = (JS_DIR / "chat.js").read_text(encoding="utf-8")
    body = re.search(r"async function onSend\(rewriteHint\)[\s\S]*?\n  \}", source)
    assert body, "未找到 onSend"
    assert re.search(r"typeof\s+rewriteHint\s*===\s*'string'", body.group(0)), (
        "onSend 未做类型过滤，事件对象等非字符串会污染 rewrite_hint"
    )
    assert "submitQuery(text, hint)" in body.group(0), (
        "onSend 未把过滤后的 hint 交给 submitQuery"
    )


# ==================== 刷新后接管进行中的查询 ====================
# 回归背景：对话进行中刷新页面，这一轮问答凭空消失。
# 原因：回答只在生成完才落库（历史里只剩提问），且 SSE 通道在断开时被无条件回收，
# 重连既拿不到 pending 中的 final，也接不上后续进度。
# 修复：加载时先读 GET /api/query/status/{sid}，在跑就用快照重建进度并重新订阅 SSE。

def _restore_session_body() -> str:
    """取出 restoreSession 函数源码，供顺序与行为断言。"""
    source = (JS_DIR / "chat.js").read_text(encoding="utf-8")
    match = re.search(r"async function restoreSession\(\)[\s\S]*?\n  \}", source)
    assert match, "未找到 restoreSession，刷新恢复流程可能被删除或重命名"
    return match.group(0)


def test_restore_session_reads_status_before_history():
    """守卫：必须先读任务快照、再读历史。

    反序会出现竞态：历史已含回答、快照仍说 processing，于是重复渲染一个等待中的气泡。
    """
    body = _restore_session_body()
    assert body.index("fetchQueryStatus()") < body.index("loadHistory()"), (
        "restoreSession 必须先读任务快照再读历史，否则会重复渲染等待中的回答气泡"
    )


def test_restore_session_reattaches_stream_for_active_task():
    """守卫：快照说任务在跑时，必须重建进度并重新订阅 SSE。"""
    body = _restore_session_body()
    assert "ACTIVE_TASK_STATUS" in body, "未用「进行中状态」判定是否需要接管"
    assert "addBotMsgSkeleton()" in body, "未补出等待中的回答气泡"
    assert "renderProgress(" in body, "未用快照的 done/running 重建阶段进度"
    assert "attachStream(" in body, "未重新订阅 SSE，刷新后这一轮问答仍会消失"


def test_restore_session_skips_when_answer_already_saved():
    """守卫：历史最后一条已是回答时不得再接管，否则会出现两个回答气泡。"""
    body = _restore_session_body()
    assert re.search(r"if\s*\(lastRole\s*===\s*'assistant'\)\s*return", body), (
        "缺少「回答已落库就不再接管」的短路判断"
    )


def test_active_task_status_covers_only_running_states():
    """守卫：只有 pending/processing 需要接管；completed/failed 一律以历史为准。"""
    source = (JS_DIR / "chat.js").read_text(encoding="utf-8")
    match = re.search(r"const ACTIVE_TASK_STATUS = \{([^}]*)\}", source)
    assert match, "未找到 ACTIVE_TASK_STATUS"
    keys = set(re.findall(r"(\w+)\s*:", match.group(1)))
    assert keys == {"pending", "processing"}, f"进行中状态集合不正确：{keys}"


def test_load_history_reports_last_message_role():
    """守卫：loadHistory 必须回报最后一条消息的角色，恢复流程据此判断回答是否已落库。"""
    source = (JS_DIR / "chat.js").read_text(encoding="utf-8")
    body = re.search(r"async function loadHistory\(\)[\s\S]*?\n  \}", source)
    assert body, "未找到 loadHistory"
    assert "lastRole" in body.group(0), "loadHistory 未回报最后一条消息的角色"
    assert "lastUserText" in body.group(0), "loadHistory 未回报最近一条提问"


def test_query_status_path_registered_in_shared_library():
    """守卫：接口路径只有一份实现（app.js），页面不得硬编码 URL。"""
    app_js = (JS_DIR / "app.js").read_text(encoding="utf-8")
    chat_js = (JS_DIR / "chat.js").read_text(encoding="utf-8")
    assert "/api/query/status/" in app_js, "公共库未登记查询状态接口"
    assert "App.PATHS.queryStatus" in chat_js, "chat.js 未使用公共库的查询状态路径"
    assert "queryStatus" in (JS_DIR / "app.js").read_text(encoding="utf-8")


def test_init_uses_restore_session_not_bare_load_history():
    """守卫：页面初始化必须走恢复流程，不能只回显历史。"""
    source = (JS_DIR / "chat.js").read_text(encoding="utf-8")
    assert re.search(r"^\s*restoreSession\(\);", source, flags=re.MULTILINE), (
        "初始化未调用 restoreSession，刷新后无法接管进行中的查询"
    )


# ==================== 手输主体名与点选等价 ====================
# 回归背景：反问给出可点选主体后，用户没点按钮，而是自己手输主体名。
# 此时前端不带 rewrite_hint，服务端落到「历史 + 模型改写」路径；而反问轮的
# 用户消息与助手消息 item_names 都是空列表，被 list_valid_history 过滤掉，
# 模型只看得见更早的话题 → 答成了「之前的问题」（实测答成安装步骤）。
# 修复：手输文本若正是候选主体，就把触发反问的那句原话作为 rewrite_hint 传下去，
# 与点选走同一条确定性拼装路径（服务端 resolve_by_hint + compose_query_with_hint）。

_CANDIDATE = "Brother HAK 180 烫金机"


def _load_subject_hint_fns():
    """从 chat.js 中提取 matchesPendingSubject / resolveSendHint 源码并返回。"""
    node = shutil.which("node")
    if not node:
        pytest.skip("未安装 node，跳过前端函数行为验证")
    source = (JS_DIR / "chat.js").read_text(encoding="utf-8")
    parts: list[str] = []
    for fn_name in ("matchesPendingSubject", "resolveSendHint"):
        match = re.search(
            rf"^  function {fn_name}\([\s\S]*?^  \}}$", source, flags=re.MULTILINE
        )
        assert match, f"chat.js 中未找到 {fn_name}，函数可能被重命名或删除"
        parts.append(match.group(0))
    return node, "\n".join(parts)


@pytest.mark.parametrize(
    ("asked", "expected"),
    [
        # 手输完整候选名 → 命中
        ("Brother HAK 180 烫金机", "true"),
        # 只输型号前缀 / 别名写法 → 命中
        ("hak180", "true"),
        ("hak-180", "true"),
        ("haK 180", "true"),
        # 自带意图 → 不得命中。否则会用上一轮反问的意图覆盖用户本轮写的「怎么保养」
        ("Brother HAK 180 烫金机怎么保养", "false"),
        ("烫金机怎么保养", "false"),
        # 无关内容 → 不命中
        ("你好", "false"),
        ("", "false"),
    ],
)
def test_matches_pending_subject_only_when_text_is_a_candidate(asked: str, expected: str):
    """守卫：只有手输文本确实对应候选主体，才算「在回答反问」。"""
    node, fn_source = _load_subject_hint_fns()
    got = _run_js_fn(node, fn_source, "matchesPendingSubject", asked, [_CANDIDATE])
    assert got == expected


def test_matches_pending_subject_false_without_candidates():
    """候选为空时一律不命中，不得凭「像主体名」放行。"""
    node, fn_source = _load_subject_hint_fns()
    got = _run_js_fn(node, fn_source, "matchesPendingSubject", _CANDIDATE, [])
    assert got == "false"


def test_resolve_send_hint_prefers_click_then_pending():
    """点选值优先；非点选时命中候选才补来源问句；其余一律为空。"""
    node, fn_source = _load_subject_hint_fns()
    pending = {"sourceQuery": "hak180是什么", "names": [_CANDIDATE]}

    def run(is_click, clicked, text, state):
        return _run_js_fn(node, fn_source, "resolveSendHint", is_click, clicked, text, state)

    # 点选：直接用点选值，不看挂起反问
    assert run(True, "hak180怎么安装", _CANDIDATE, pending) == "hak180怎么安装"
    # 点选但点选值为空（用户只是在指主体）→ 仍为空，不得回退到挂起反问
    assert run(True, "", _CANDIDATE, pending) == ""
    # 手输命中候选 → 补上触发反问的那句原话
    assert run(False, "", _CANDIDATE, pending) == "hak180是什么"
    # 手输未命中 → 不补，交回模型改写
    assert run(False, "", "你好", pending) == ""
    # 没有挂起反问 → 不补
    assert run(False, "", _CANDIDATE, None) == ""


def test_on_send_consumes_pending_subject_ask_once():
    """守卫：挂起反问必须在 onSend 里无条件消费掉，避免残留影响后续提问。"""
    source = (JS_DIR / "chat.js").read_text(encoding="utf-8")
    body = re.search(r"async function onSend\(rewriteHint\)[\s\S]*?\n  \}", source)
    assert body, "未找到 onSend"
    text = body.group(0)
    assert "pendingSubjectAsk = null" in text, "onSend 未消费挂起的反问"
    assert text.index("pendingSubjectAsk = null") < text.index("resolveSendHint("), (
        "必须先清空挂起的反问，再算本轮 hint"
    )


def test_render_item_name_options_records_pending_subject_ask():
    """守卫：渲染选项栏时必须记下来源问句，否则手输路径拿不到意图。"""
    source = (JS_DIR / "chat.js").read_text(encoding="utf-8")
    body = re.search(r"function renderItemNameOptions\([\s\S]*?\n  \}", source)
    assert body, "未找到 renderItemNameOptions"
    assert "pendingSubjectAsk = {" in body.group(0), "渲染选项栏时未记录挂起的反问"
    assert "sourceQuery" in body.group(0), "挂起的反问缺少来源问句"

