"""重排服务：合并 RRF 与联网结果 → BGE-Reranker 打分 → 动态截断 → 回写 ``reranked_docs``。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from langchain_core.messages import HumanMessage
from langchain_core.output_parsers import StrOutputParser

from app.process.query.agent.state import QueryGraphState
from app.rag.query.config import (
    RERANK_GAP_ABS,
    RERANK_GAP_RATIO,
    RERANK_MAX_INPUT_TOKENS,
    RERANK_MIN_SUMMARY_CHARS,
    RERANK_MIN_TOPK,
    RERANK_SUMMARY_CHAR_RATIO,
    RERANK_SUMMARY_MAX_WORKERS,
    WEB_MAX_IN_CONTEXT,
    get_rerank_topk,
)
from app.shared.models import llm_providers
from app.shared.runtime.logger import logger, step_log
from app.shared.runtime.prompts import load_prompt
from app.shared.utils.rate_limit import apply_api_rate_limit
from app.shared.utils.require import require_state_list, require_state_str


@step_log("_require_rerank_inputs")
def _require_rerank_inputs(state: QueryGraphState) -> tuple[str, list, list]:
    """校验重排输入：``rewritten_query`` / ``rrf_chunks`` 为硬依赖，联网结果允许为空。"""
    rewritten_query = require_state_str(state, "rewritten_query")
    rrf_chunks = require_state_list(state, "rrf_chunks")
    web_search_docs = require_state_list(state, "web_search_docs", allow_empty=True)
    if not web_search_docs:
        logger.warning("web_search_docs 为空，本次仅使用本地召回结果参与重排")
    return rewritten_query, rrf_chunks, web_search_docs


@step_log("deal_rrf_and_web_result")
def deal_rrf_and_web_result(rrf_chunks: list, web_search_docs: list) -> list[dict]:
    """把本地召回与联网结果统一成重排输入结构（text/title/score/type/url）。"""
    reranker_docs = [
        {
            "chunk_id": chunk.get("chunk_id"),
            # 自进化条目是「问题 + 答案」结构：只喂答案会让很短的答案（如“配对码123456”）
            # 与问句不匹配、被重排压低甚至截断；带上 FAQ 问题才符合它的语义。
            "text": (
                f"{chunk.get('title')}\n{chunk.get('content')}"
                if chunk.get("source") == "evolution" and chunk.get("title")
                else chunk.get("content")
            ),
            "title": chunk.get("title"),
            "score": 0,  # 占位：稍后由 reranker 打分覆盖
            "type": "milvus",
            "source": chunk.get("source"),  # 保留来源标志（milvus/evolution）供引用回填
            "url": None,
        }
        for chunk in rrf_chunks
    ]
    reranker_docs.extend(
        {
            "chunk_id": None,
            "text": doc.get("snippet"),
            "title": doc.get("title"),
            "score": 0,
            "type": "web",
            "source": "web",
            "url": doc.get("url"),
        }
        for doc in web_search_docs
    )
    return reranker_docs


@step_log("cap_web_docs")
def cap_web_docs(docs: list[dict], *, limit: int = WEB_MAX_IN_CONTEXT) -> list[dict]:
    """限制联网结果条数：本地有命中时联网只作补充，避免把本地知识挤出上下文。"""
    local = [d for d in docs if d.get("type") != "web"]
    web = [d for d in docs if d.get("type") == "web"]
    if not local or len(web) <= limit:
        return docs
    logger.info(f"联网结果 {len(web)} 条、本地命中 {len(local)} 条 → 仅保留前 {limit} 条联网结果")
    keep = {id(d) for d in local} | {id(d) for d in web[:limit]}
    return [d for d in docs if id(d) in keep]


@step_log("prefer_local_docs")
def prefer_local_docs(docs: list[dict]) -> list[dict]:
    """本地知识优先：联网结果不得排在本地命中之前。

    真实事故：某次提问的 4 条联网结果分数 0.999x，压过本地手册切片排到最前，
    答案便主要摘自媒体片段——而联网来源不参与引用回填，界面表现为「答了却无引用、
    置信度 0%」。``cap_web_docs`` 只限制了联网条数，限制不了排名，这里补上排名约束：

    - 本地（知识库 / 自进化）无命中时，联网结果保持原分（纯联网问答仍可用）；
    - 本地有命中时，联网文档的排序分被压到「不超过本地最高分」，同分时本地排前
      （排序键第二项显式标注来源，不依赖排序稳定性）。
    """
    local_scores = [float(d.get("score") or 0.0) for d in docs if d.get("type") != "web"]
    if local_scores:
        best_local = max(local_scores)
        clamped = 0
        for doc in docs:
            if doc.get("type") == "web" and float(doc.get("score") or 0.0) > best_local:
                doc["score"] = best_local
                clamped += 1
        if clamped:
            logger.info(f"联网结果 {clamped} 条分数被压至本地最高分 {best_local:.4f}，本地知识优先排序")
    return sorted(
        docs,
        key=lambda d: (float(d.get("score") or 0.0), d.get("type") != "web"),
        reverse=True,
    )


@step_log("ensure_evolution_docs")
def ensure_evolution_docs(docs: list[dict], candidates: list[dict]) -> list[dict]:
    """权威条目保底：自进化条目是人工审批过的知识，不能被重排截断丢掉。

    :param docs: 动态截断后的最终上下文
    :param candidates: 截断前的完整候选（用于找回被切掉的权威条目）

    与 RRF 阶段的「防挤出」对齐，否则会出现「审批入库了，但重排把 FAQ 切掉、客服仍答不出」。
    """
    authority = [d for d in candidates if d.get("source") == "evolution"]
    if not authority:
        return docs
    existing = {d.get("chunk_id") for d in docs}
    missing = [d for d in authority if d.get("chunk_id") not in existing]
    if not missing:
        return docs
    logger.info(f"重排截断后补回 {len(missing)} 条自进化权威条目：{[d.get('chunk_id') for d in missing]}")
    return missing + docs


def _summarize_for_rerank(rewritten_query: str, answer: str, limit: int) -> str:
    """超长文本压缩：仅用于重排打分，不改变最终答案上下文。"""
    apply_api_rate_limit()
    prompt_text = load_prompt("rerank_text_refine", question=rewritten_query, answer=answer, limit=limit)
    chain = llm_providers.chat() | StrOutputParser()
    return chain.invoke([HumanMessage(content=prompt_text)])


def _encode_capped(tokenizer, text: str, max_tokens: int) -> list:
    """按上限截断编码。

    直接 ``tokenizer.encode(超长文本)`` 会让 transformers 打印
    「Token indices sequence length is longer than ...」告警（旧实现每次重排都会刷），
    这里统一走 ``truncation=True`` 的受限编码：既拿到判断所需长度，又不产生告警。
    """
    return tokenizer.encode(
        str(text or ""),
        add_special_tokens=False,
        truncation=True,
        max_length=max(max_tokens, 1),
    )


def _hard_truncate(tokenizer, text: str, max_tokens: int) -> str:
    """按 token 硬截断（LLM 压缩结果长度不可控，超长会让重排模型报索引越界）。"""
    if max_tokens <= 0:
        return ""
    tokens = _encode_capped(tokenizer, text, max_tokens)
    if len(tokens) < max_tokens:
        return text  # 未超限，保留原文
    return tokenizer.decode(tokens[:max_tokens])


@step_log("create_question_answer_lists")
def create_question_answer_lists(rewritten_query: str, reranker_docs: list[dict]) -> list[list[str]]:
    """组装 ``[问题, 答案]`` 列表；超长答案先压缩到重排模型可接受的长度。

    压缩需要调用 LLM，文档较多时串行会明显拖慢重排；此处用有界线程池并发（默认 4），
    任一环节失败自动回退为原文本，保证重排仍可继续。
    """
    tokenizer = llm_providers.reranker_model().tokenizer
    query_token_len = len(_encode_capped(tokenizer, rewritten_query, RERANK_MAX_INPUT_TOKENS))
    # reranker 固定 4 个分隔符 token，为「问题 + 分隔符」预留后再算可用长度
    available = RERANK_MAX_INPUT_TOKENS - 4 - query_token_len
    limit = max(RERANK_MIN_SUMMARY_CHARS, int(available / RERANK_SUMMARY_CHAR_RATIO))

    pairs: list[list[str]] = []
    tasks: list[tuple[int, str]] = []
    for index, doc in enumerate(reranker_docs):
        answer = doc.get("text") or ""
        answer_token_len = len(_encode_capped(tokenizer, answer, RERANK_MAX_INPUT_TOKENS))
        if query_token_len + answer_token_len + 4 > RERANK_MAX_INPUT_TOKENS:
            tasks.append((index, answer))
        pairs.append([rewritten_query, answer])

    if tasks:
        max_workers = min(RERANK_SUMMARY_MAX_WORKERS, len(tasks))
        try:
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                results = list(pool.map(
                    lambda item: _summarize_for_rerank(rewritten_query, item[1], limit),
                    tasks,
                ))
            for (index, _), summary in zip(tasks, results):
                if summary:
                    pairs[index][1] = summary
        except Exception as exc:  # noqa: BLE001 - 压缩属优化项，失败则退回原文
            logger.warning(f"重排长文本压缩失败，回退原文打分：{exc}")

    # 压缩结果长度不可控（或压缩失败），统一按 token 预算硬截断，保证输入不超模型上限
    budget = RERANK_MAX_INPUT_TOKENS - 4 - query_token_len
    for pair in pairs:
        pair[1] = _hard_truncate(tokenizer, pair[1], budget)
    return pairs


@step_log("use_reranker_deal_score")
def use_reranker_deal_score(question_answer_pair_list: list, reranker_docs: list[dict]) -> None:
    """调用重排模型打分并按分数倒序排序（原地修改 ``reranker_docs``）。"""
    scores_list = llm_providers.reranker_model().compute_score(question_answer_pair_list, normalize=True)
    for score, doc in zip(scores_list, reranker_docs):
        doc["score"] = score
    reranker_docs.sort(key=lambda x: x.get("score", 0), reverse=True)


@step_log("dyn_limit_reranker_docs")
def dyn_limit_reranker_docs(reranker_docs: list[dict]) -> list[dict]:
    """动态截断：以重排峰值作基准逐项累计下跌，达到阈值即截断。

    相比仅比较相邻两项，累计口径能捕捉「相邻分差细小、但相对头部已明显下滑」的慢坡，
    避免把头尾质量接近的低分长尾一并放进作答上下文。前 ``RERANK_MIN_TOPK`` 条无条件保留。
    """
    if not reranker_docs:
        return []
    top_max = min(get_rerank_topk(), len(reranker_docs))
    topk = top_max
    running_max = reranker_docs[0].get("score", 0.0)

    for i in range(RERANK_MIN_TOPK, top_max):
        score = reranker_docs[i].get("score", 0.0)
        if score > running_max:
            running_max = score
            continue
        abs_score = running_max - score
        ratio = abs_score / running_max if running_max else 0.0
        if abs_score > RERANK_GAP_ABS or ratio > RERANK_GAP_RATIO:
            topk = i
            break
    return reranker_docs[:topk]


@step_log("rerank_documents")
def rerank_documents(state: QueryGraphState) -> QueryGraphState:
    """重排服务入口：合并 → 压缩 → 打分排序 → 动态截断 → 回写 ``reranked_docs``。"""
    rewritten_query, rrf_chunks, web_search_docs = _require_rerank_inputs(state)
    reranker_docs = deal_rrf_and_web_result(rrf_chunks, web_search_docs)
    question_answer_pair_list = create_question_answer_lists(rewritten_query, reranker_docs)
    use_reranker_deal_score(question_answer_pair_list, reranker_docs)
    # 本地知识优先排序 → 联网只作补充（限条数）→ 动态截断 → 权威条目保底
    reranker_docs = prefer_local_docs(reranker_docs)
    reranker_docs = cap_web_docs(reranker_docs)
    scored_docs = list(reranker_docs)  # 截断前的完整候选（权威条目保底用）
    reranker_docs = dyn_limit_reranker_docs(reranker_docs)
    reranker_docs = ensure_evolution_docs(reranker_docs, scored_docs)
    state["reranked_docs"] = reranker_docs
    logger.info(
        "重排完成：候选 {} 条，保留 {} 条；Top3={}".format(
            len(question_answer_pair_list),
            len(reranker_docs),
            [
                {"chunk_id": d.get("chunk_id"), "score": round(float(d.get("score") or 0.0), 4)}
                for d in reranker_docs[:3]
            ],
        )
    )
    return state
