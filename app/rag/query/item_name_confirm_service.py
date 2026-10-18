"""主体确认服务（查询链路首节点）。

职责：结合历史对话提取商品名 → 改写问题 → 在主体名集合中混合检索并打分 →
自动确认标准主体，或生成反问让用户确认；同时把本轮提问写入会话历史。
"""
from __future__ import annotations

import re

from langchain_core.messages import HumanMessage

from app.process.query.agent.state import QueryGraphState
from app.rag.item_name.match import search_by_item_names, select_item_names, similar_from_query
from app.rag.query.history_utils import build_history_context
from app.shared.clients.history_repository import history_repository
from app.shared.models import llm_providers
from app.shared.runtime.logger import logger, step_log
from app.shared.runtime.prompts import load_prompt
from app.shared.utils.json_utils import parse_json_object
from app.shared.utils.require import require_state_str


@step_log("get_history_messages_and_context")
def get_history_messages_and_context(session_id: str) -> str:
    """获取近期有效历史对话并拼接为提示词上下文片段。"""
    return build_history_context(session_id, limit=10)


@step_log("call_llm_item_name_and_rewritten")
def call_llm_item_name_and_rewritten(history_text: str, original_query: str) -> dict:
    """调用模型识别主体名并改写问题（历史上下文放在规则之后，避免干扰规则读取）。"""
    json_llm_client = llm_providers.chat(json_mode=True)
    history_prompt_text = load_prompt(
        "rewritten_query_and_itemnames", query=original_query, history_text=history_text
    )
    response = json_llm_client.invoke([HumanMessage(content=history_prompt_text)])
    result_dict = parse_json_object(response.content)
    # 参数校验赋予默认值：模型可能只返回其中一个字段
    result_dict.setdefault("item_names", [])
    if not result_dict.get("rewritten_query"):
        result_dict["rewritten_query"] = original_query
    return result_dict


@step_log("compose_query_with_hint")
def compose_query_with_hint(subject: str, hint: str) -> str:
    """把「主体名」与「意图原句」拼成一条完整问句。

    意图原句里常自带主体别名（如 “hak180怎么安装”），直接拼会得到
    “Brother HAK 180 烫金机hak180怎么安装” 这种重复且拗口的问句，
    既影响检索质量也影响最终答案的行文。故先把原句里能识别出的主体别名剔除，
    只保留真正的意图部分（如 “怎么安装”）。

    :return: 拼好的问句；两侧输入都为空时返回空串（调用方据此判定「无问句可用」）。
    """
    subject = (subject or "").strip()
    hint = (hint or "").strip()
    if not hint:
        return subject
    if not subject:
        return hint

    compact = lambda s: re.sub(r"[\s\W_]+", "", s).lower()  # noqa: E731 - 局部小工具
    compact_subject = compact(subject)

    # 原句已含完整主体名：无需再拼，直接用它
    if compact_subject and compact_subject in compact(hint):
        return hint

    # 剔除原句里的主体别名（型号片段 / 品类词）：剩下的就是纯意图
    remainder = _strip_subject_alias(hint, subject)
    if not remainder:
        return subject
    return f"{subject}{remainder}"


def _strip_subject_alias(hint: str, subject: str) -> str:
    """从意图原句中剔除主体别名，返回剩余的意图文字。

    别名有两类，都要剔掉，否则会拼出 “Brother HAK 180 烫金机烫金机如何保养” 这类重复：

    1. 型号片段：主体名里的字母数字组合（如 “HAK 180” → “hak180”）；
    2. 品类/品牌词：如 “烫金机”“打印机”——它们是主体的组成部分，
       用户往往用它们指代同一台设备。

    命中位置前后的文字拼接后即为纯意图（如 “hak180烫金机怎么换膜” → “怎么换膜”）。
    """
    remainder = hint
    # ① 型号片段（字母数字组合），按长度降序避免短片段先命中造成误伤
    tokens = re.findall(r"[A-Za-z]+\s*\d+|\d+\s*[A-Za-z]+|[A-Za-z]+", subject)
    for token in sorted(tokens, key=len, reverse=True):
        token_key = re.sub(r"\s+", "", token)
        if len(token_key) < 3:
            continue  # 过短的片段容易误伤正文
        remainder = _drop_first_match(remainder, token_key, fuzzy_case=True)
    # ② 品类/品牌词（连续中文），同样按长度降序
    for word in sorted(re.findall(r"[\u4e00-\u9fff]{2,}", subject), key=len, reverse=True):
        remainder = _drop_first_match(remainder, word)
    return remainder.strip()


def _drop_first_match(text: str, needle: str, *, fuzzy_case: bool = False) -> str:
    """删掉 ``text`` 中第一次出现的 ``needle``（跳过其中的空白与常见分隔符），返回剩余部分。

    未命中时原样返回。命中后剩余部分为空也照常返回空串——调用方据此判断
    「原句实际只是主体名」，进而退回主体名本身。

    ``needle`` 里可能含连字符（型号常见的 ``HAK-180`` 写法），而用户输入时
    未必带；故把 ``-`` ``_`` ``·`` 也纳入「可跳过字符」，让 ``haK-180`` 同样能命中。
    """
    if not needle:
        return text
    pattern = r"[\s\-_·\.]*".join(re.escape(ch) for ch in needle)
    flags = re.IGNORECASE if fuzzy_case else 0
    match = re.search(pattern, text, flags)
    if not match:
        return text
    return text[: match.start()] + text[match.end():]


def _align_subject(query: str) -> str | None:
    """把展示名/别名对齐到库内标准主体名；对齐不上返回 None。

    两条消解路径都要这步：用户给的是展示名，必须映射到目录标准名才能命中过滤条件。
    """
    list_dict = select_item_names(search_by_item_names([query]))
    confirmed_list = list_dict.get("confirmed_list", [])
    if not confirmed_list:
        return None
    return confirmed_list[0].get("item_name")


def _apply_confirmed_subject(state: QueryGraphState, confirmed_name: str, hint: str) -> None:
    """回写「已确认主体 + 拼装问句」（两条消解路径共用）。

    必须清 options 与 answer：前者否则本轮仍被当作待确认；后者否则路由短路去答反问话术。
    """
    state["item_names"] = [confirmed_name]
    state["rewritten_query"] = compose_query_with_hint(confirmed_name, hint)
    state["item_name_options"] = []
    if "answer" in state:
        state["answer"] = None


def resolve_by_hint(state: QueryGraphState) -> bool:
    """点选主体后的确定性消解：直接用主体名 + 意图原句作答，不经过模型改写。

    为什么不让模型做：实测中即便提示词明令「意图以当前问题为准」，当问句里只有
    主体名（如用户点了主体）而历史在聊别的话题时，模型仍会把它改写成历史话题，
    表现为「点选主体后回答的是上一个问题」。这里前端已把意图单独传上来，
    模型没有推断的余地，直接拼装最可靠。

    主体名仍要走一次目录对齐（``search_by_item_names``/``select_item_names``），
    因为前端传的是展示名，需要映射到库内标准主体上方能命中过滤条件。

    :return: 已处理（成功确认主体）返回 True；未能对齐主体则返回 False，
        由调用方回退到常规的「历史 + 模型改写 + 相似主体」流程。
    """
    hint = (state.get("rewrite_hint") or "").strip()
    original_query = require_state_str(state, "original_query")
    if not hint:
        return False

    # 前端把主体名放在 query；取回后与意图拼成完整问句
    confirmed_name = _align_subject(original_query)
    if not confirmed_name:
        logger.info(f"点选消解：主体未能对齐目录，回退常规流程：query={original_query}")
        return False

    _apply_confirmed_subject(state, confirmed_name, hint)
    logger.info(
        f"点选消解完成：主体={confirmed_name}，意图原句={hint}，"
        f"拼装问句={state['rewritten_query']}"
    )
    return True


def find_pending_clarify(session_id: str) -> dict:
    """最近一条消息若是「带候选的助手反问」，返回其 pending 记录；否则空字典。

    本轮 user 消息在 ``confirm_item_name`` 末尾才落库，故此处 ``limit=1`` 拿到的是
    上一轮助手消息；首轮无历史返回空字典。只认最近一条，避免更早的反问残留成
    「上一轮意图」——那正是「答成之前的问题」的成因。
    """
    recent = history_repository.list_recent(session_id, limit=1)
    if not recent:
        return {}
    last = recent[0]
    if last.get("role") != "assistant":
        return {}
    pending = last.get("pending_subject_ask") or {}
    if not (pending.get("intent") and pending.get("options")):
        return {}
    return pending


def resolve_by_pending_clarify(state: QueryGraphState) -> bool:
    """手输纯主体名的服务端兜底：用上一轮反问的意图原话复原问句。

    覆盖前端挂起态失效的两类场景：页面刷新后重发、非浏览器客户端。
    四道闸门任一不过就让路给常规的「历史 + 模型改写」流程：

    ① 上一轮确实是反问（读历史，最便宜，先做）；
    ② 本轮 query 能对齐到目录标准主体（判「是否纯主体名」要标准名，故须在③之前）；
    ③ 该主体 ∈ 反问给出的候选（只认候选，避免把「另一台设备」硬拼成垃圾问句）；
    ④ 本轮去掉主体别名后为空，即用户没带自己的意图。
    """
    original_query = require_state_str(state, "original_query")

    # 闸门①：便宜的门——绝大多数轮次上一轮不是反问，直接早退，不白跑向量检索
    pending = find_pending_clarify(state.get("session_id"))
    if not pending:
        return False

    confirmed_name = _align_subject(original_query)
    if not confirmed_name:
        return False

    if confirmed_name not in set(pending["options"]):
        return False

    # 闸门④：本轮自带意图（如「…烫金机怎么保养」）→ 尊重用户，不接管
    if _strip_subject_alias(original_query, confirmed_name):
        return False

    _apply_confirmed_subject(state, confirmed_name, pending["intent"])
    logger.info(
        f"手输兜底消解完成：主体={confirmed_name}，反问意图原话={pending['intent']}，"
        f"拼装问句={state['rewritten_query']}"
    )
    return True


@step_log("apply_item_name_result")
def apply_item_name_result(state: QueryGraphState, list_dict: dict[str, list], rewritten_query: str) -> None:
    """根据「确认 / 可选 / 相似」三类结果改写 state 的 answer、item_names 与选项。

    - 确认列表非空：正常进入多路召回（answer 置空）；
    - 可选列表非空：写入候选主体与改写问题，并把候选作为**可点选选项**回给用户；
    - 相似列表非空：没到阈值（如只输入型号前缀 ``hak180``）时列出相似主体供点选；
    - 都没有：提示用户补充主体后再提问。

    选项会放进 ``state["item_name_options"]``，由 API/SSE 透出、前端渲染成按钮，
    用户点选即可直接以该主体重新提问——不再只回一句「请您明确主体再提问」。
    """
    confirmed_list = list_dict.get("confirmed_list", [])
    option_list = list_dict.get("option_list", [])
    similar_list = list_dict.get("similar_list", [])
    state["item_name_options"] = []

    if confirmed_list:
        state["item_names"] = [item.get("item_name") for item in confirmed_list]
        state["rewritten_query"] = rewritten_query
        if "answer" in state:
            state["answer"] = None
        return

    if option_list or similar_list:
        candidates = option_list or similar_list
        state["item_name_options"] = candidates
        # 候选主体只作为可点选选项回给用户，**不写入 item_names、不随用户消息落库**：
        # 未确认候选一旦进历史，下一轮 list_valid_history 会把它送进主体消解与答案
        # 上下文，模型可能把「候选」当「已确认主体」误消解。
        # 用户点选选项后，前端会以「主体名」+ 独立的 rewrite_hint 重新提问
        # （chat.js askSubject → optionQuestion / optionIntentHint），下一轮由
        # resolve_by_hint 确定性拼装，不依赖历史回写，也不经过模型推断。
        candidate_names: list[str] = []
        for item in candidates:
            name = item.get("item_name")
            if name and name not in candidate_names:
                candidate_names.append(name)
        if rewritten_query:
            state["rewritten_query"] = rewritten_query
        if option_list:
            state["answer"] = (
                f"本次提问没有确认主体，但有相似可选的：{'、'.join(candidate_names)}。"
                "请点击下方主体直接提问，或补充完整名称后继续。"
            )
        else:
            state["answer"] = (
                f"没有识别到明确的主体，以下可能是您要找的：{'、'.join(candidate_names)}。"
                "请点击下方主体直接提问。"
            )
        return

    state["answer"] = "本次问题没有关联到任何主体，也没有找到相似主体。请补充产品名称后再提问。"


@step_log("save_history_message")
def save_history_message(state: QueryGraphState) -> None:
    """把本轮用户提问写入会话历史（供下一轮主体消解使用）。"""
    history_repository.save_message(
        session_id=state.get("session_id"),
        role="user",
        text=state.get("original_query"),
        rewritten_query=state.get("rewritten_query", ""),
        item_names=state.get("item_names", []),
        image_urls=[],
        intent="rag",
        turn_id=state.get("turn_id", ""),
    )


@step_log("confirm_item_name")
def confirm_item_name(state: QueryGraphState) -> QueryGraphState:
    """主体确认服务入口：1) 历史上下文 → 2) LLM 提取主体与改写 → 3) 向量对齐 → 4) 回写 state。

    确定性消解优先于模型改写，两条路径：
    - ``resolve_by_hint``：本轮带 ``rewrite_hint``（前端点选主体，或手输命中反问候选）；
    - ``resolve_by_pending_clarify``：本轮没带 hint，但上一轮反问过且本轮是候选主体名。
      前端挂起态存在内存里、刷新即丢，这条兜底覆盖刷新与非浏览器客户端。

    两者都不接管时，才走「历史 + 模型改写 + 相似主体」的常规流程。
    """
    session_id = require_state_str(state, "session_id")
    original_query = require_state_str(state, "original_query")
    logger.info(f"主体确认开始：session_id={session_id}, query={original_query}")

    if resolve_by_hint(state):
        save_history_message(state)
        return state

    # 前端挂起态失效（刷新/非浏览器）时的手输兜底：上一轮反问过且本轮是候选主体名
    if resolve_by_pending_clarify(state):
        save_history_message(state)
        return state

    history_text = get_history_messages_and_context(session_id)
    result = call_llm_item_name_and_rewritten(history_text, original_query)

    list_dict: dict[str, list] = {}
    if result.get("item_names"):
        milvus_result = search_by_item_names(result["item_names"])
        list_dict = select_item_names(milvus_result)
    else:
        # 模型没抽出主体（例如只说了品类「烫金机」）：仍然要用**问句本身**在目录里找相似主体，
        # 否则同一界面上「hak180 给选项、烫金机怎么安装 只回请补充产品名称」，用户无法理解。
        similar = similar_from_query(original_query, result.get("rewritten_query") or "")
        if similar:
            list_dict = {"confirmed_list": [], "option_list": [], "similar_list": similar}
            logger.info(
                f"模型未抽出主体，按问句给出相似主体供点选：{[item['item_name'] for item in similar]}"
            )
        else:
            logger.info(f"模型未抽出主体，且问句中未出现库内关键词：query={original_query}")

    apply_item_name_result(state, list_dict, result.get("rewritten_query"))
    save_history_message(state)
    return state
