"""重排服务：合并 RRF 与联网结果 → BGE-Reranker 打分 → 动态截断 → 回写 ``reranked_docs``。"""
from __future__ import annotations

from app.process.query.agent.state import QueryGraphState
from app.rag.query.business import classify_business_request
from app.rag.query.config import (
    LEGACY_ANSWER_RESERVE_SECONDS,
    RERANK_GAP_ABS,
    RERANK_GAP_RATIO,
    RERANK_MAX_INPUT_TOKENS,
    RERANK_MIN_LOCAL_TOPK,
    RERANK_MIN_TOPK,
    WEB_MAX_IN_CONTEXT,
    get_rerank_topk,
    get_rrf_top,
)
from app.rag.query.eval_evidence import chunk_ids
from app.rag.query.manual_evidence_rerank import keep_procedure_context, keep_retrieval_agreement, rank_manual_evidence
from app.rag.query.manual_keyword_search import matches_requested_heading
from app.shared.models import llm_providers
from app.shared.models.reranker import reranker_tokenizer_lock, score_pairs
from app.shared.runtime.deadline import remaining
from app.shared.runtime.logger import logger, step_log
from app.shared.runtime.query_metrics import current_metrics, mark_degraded, measure_stage
from app.shared.utils.require import require_state_list, require_state_str


@step_log("_require_rerank_inputs")
def _require_rerank_inputs(state: QueryGraphState) -> tuple[str, list, list]:
    """校验重排输入：``rewritten_query`` 为硬依赖，两路结果允许为空（降级/零命中路径）。"""
    rewritten_query = require_state_str(state, "rewritten_query")
    rrf_chunks = require_state_list(state, "rrf_chunks", allow_empty=True)
    web_search_docs = require_state_list(state, "web_search_docs", allow_empty=True)
    if not web_search_docs:
        logger.warning("web_search_docs 为空，本次仅使用本地召回结果参与重排")
    return rewritten_query, rrf_chunks, web_search_docs


@step_log("deal_rrf_and_web_result")
def deal_rrf_and_web_result(rrf_chunks: list, web_search_docs: list) -> list[dict]:
    """把本地召回与联网结果统一成重排输入结构（text/title/score/type/url）。

    同时透传 ``parent_id`` / ``page`` / ``heading_path`` / ``file_title``：本函数会重建 dict，
    若不带过去，后续的父块回溯（``node_parent_expand``）就没有分组键可用。
    """
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
            "parent_id": chunk.get("parent_id"),
            "doc_id": chunk.get("doc_id"),
            "page": chunk.get("page"),
            "seq": chunk.get("seq"),
            "heading_path": chunk.get("heading_path"),
            "file_title": chunk.get("file_title"),
            "document_kind": chunk.get("document_kind", "manual"),
            "document_version": chunk.get("document_version", ""),
            "valid_until": chunk.get("valid_until", 0.0),
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


def _encode_capped(tokenizer, text: str, max_tokens: int) -> list:
    """按上限截断编码。

    直接 ``tokenizer.encode(超长文本)`` 会让 transformers 打印
    「Token indices sequence length is longer than ...」告警（旧实现每次重排都会刷），
    这里统一走 ``truncation=True`` 的受限编码：既拿到判断所需长度，又不产生告警。
    """
    with reranker_tokenizer_lock:
        return tokenizer.encode(
            str(text or ""),
            add_special_tokens=False,
            truncation=True,
            max_length=max(max_tokens, 1),
        )


def _hard_truncate(tokenizer, text: str, max_tokens: int) -> tuple[str, int]:
    """返回限长文本和实际保留的 token 数。"""
    tokens = _encode_capped(tokenizer, text, max_tokens)
    return (text if len(tokens) < max_tokens else tokenizer.decode(tokens)), len(tokens)


@step_log("create_question_answer_lists")
def create_question_answer_lists(rewritten_query: str, reranker_docs: list[dict]) -> list[list[str]]:
    """按本地模型的 token 预算构造重排输入。保留手册章节路径。"""
    tokenizer = llm_providers.reranker_model().tokenizer
    query, query_tokens = _hard_truncate(tokenizer, rewritten_query, RERANK_MAX_INPUT_TOKENS * 3 // 4)
    fixed_tokens = query_tokens + 4
    budget = RERANK_MAX_INPUT_TOKENS - fixed_tokens
    pairs: list[list[str]] = []
    total_tokens = 0
    for doc in reranker_docs:
        text = doc.get("text") or ""
        heading = doc.get("heading_path")
        if heading:
            text = f"{heading}\n{text}"
        passage, tokens = _hard_truncate(tokenizer, text, budget)
        pairs.append([query, passage])
        total_tokens += fixed_tokens + tokens
    metrics = current_metrics.get()
    if metrics is not None:
        with metrics.lock:
            metrics.rerank_pairs = len(pairs)
            metrics.rerank_input_tokens = total_tokens
    return pairs


@step_log("use_reranker_deal_score")
def use_reranker_deal_score(question_answer_pair_list: list, reranker_docs: list[dict]) -> None:
    """调用重排模型打分并按分数倒序排序（原地修改 ``reranker_docs``）。"""
    scores_list = score_pairs(llm_providers.reranker_model(), question_answer_pair_list)
    for score, doc in zip(scores_list, reranker_docs):
        doc["score"] = score
    reranker_docs.sort(key=lambda x: x.get("score", 0), reverse=True)


@step_log("dyn_limit_reranker_docs")
def dyn_limit_reranker_docs(reranker_docs: list[dict]) -> list[dict]:
    """动态截断：以重排峰值作基准逐项累计下跌，达到阈值即截断。

    相比仅比较相邻两项，累计口径能捕捉「相邻分差细小、但相对头部已明显下滑」的慢坡，
    避免把头尾质量接近的低分长尾一并放进作答上下文。
    优先保留前五条本地证据，并遵守总条数上限。分差阈值只截断剩余候选。
    """
    if not reranker_docs:
        return []
    top_max = min(get_rerank_topk(), len(reranker_docs))
    required_local = [doc for doc in reranker_docs if doc.get('type') != 'web'][:min(RERANK_MIN_LOCAL_TOPK, top_max)]
    selected_ids = {id(doc) for doc in required_local}
    for doc in reranker_docs:
        if len(selected_ids) >= top_max:
            break
        selected_ids.add(id(doc))
    reranker_docs = [doc for doc in reranker_docs if id(doc) in selected_ids]
    topk = top_max
    running_max = reranker_docs[0].get("score", 0.0)
    minimum_local = len(required_local)

    for i in range(RERANK_MIN_TOPK, top_max):
        if minimum_local and sum(doc.get('type') != 'web' for doc in reranker_docs[:i]) < minimum_local:
            continue
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
    """选择手册证据。其他候选由本地模型打分和截断。"""
    rewritten_query, rrf_chunks, web_search_docs = _require_rerank_inputs(state)

    if not rrf_chunks and not web_search_docs:
        # 全链路零命中（检索降级）：不加载重排模型，直接按空结果进入作答，
        # 由 answer_out prompt 的无结果兜底规则生成「无法作答」话术。
        # 只放宽 RRF 不够——这里会在 require_state_list 再次抛错仍是 500。
        logger.warning("本地召回与联网结果均为空，跳过重排，按零命中作答")
        state["reranked_docs"] = []
        return state

    reranker_docs = cap_web_docs(deal_rrf_and_web_result(rrf_chunks, web_search_docs))
    local = [doc for doc in reranker_docs if doc["type"] != "web"]
    web = [doc for doc in reranker_docs if doc["type"] == "web"]
    local_limit = min(RERANK_MIN_LOCAL_TOPK, get_rerank_topk())
    use_manual_ranking = (
        local_limit > 0 and len(local) > RERANK_MIN_LOCAL_TOPK
        and classify_business_request(state) in ("product_knowledge", "troubleshooting")
        and all(doc["source"] == "milvus" and doc["document_kind"] in (None, "", "manual") for doc in local)
    )
    if use_manual_ranking:
        question = state.get("rewrite_hint") or state.get("original_query") or rewritten_query
        ranked = rank_manual_evidence(state, local, local_limit)
        if ranked is not None:
            state["reranked_chunk_ids"] = chunk_ids(ranked)
        else:
            keyword = state.get("keyword_chunks") or []
            available = remaining()
            if keyword or (available is not None and available <= LEGACY_ANSWER_RESERVE_SECONDS):
                mark_degraded("manual_evidence_fusion_fallback")
                selected = local[:local_limit]
                if keyword:
                    leader = keyword[0].get("chunk_id")
                    protected = selected[-1]
                    if (leader and leader not in chunk_ids(selected)
                            and not matches_requested_heading(protected, question)):
                        candidate = next((doc for doc in local if doc.get("chunk_id") == leader), None)
                        if candidate is not None:
                            selected[-1] = candidate
                ranked = [dict(doc, score=None) for doc in selected]
        if ranked is not None:
            selected = keep_procedure_context(question, ranked, local)
            selected = keep_retrieval_agreement(state, selected, local, local_limit)
            state["reranked_docs"] = (selected + [dict(doc, score=None) for doc in web])[:get_rerank_topk()]
            return state
        # 关键词分支没有候选时，限制本地重排的输入条数。
        semantic_limit = max(get_rrf_top(), RERANK_MIN_LOCAL_TOPK)
        reranker_docs = local[:semantic_limit + 2] + web

    state["local_rerank_input_chunk_ids"] = chunk_ids(reranker_docs)
    with measure_stage("rerank_prepare"):
        question_answer_pair_list = create_question_answer_lists(rewritten_query, reranker_docs)
    with measure_stage("local_rerank"):
        use_reranker_deal_score(question_answer_pair_list, reranker_docs)
    # 本地知识优先排序 → 动态截断 → 权威条目保底
    reranker_docs = prefer_local_docs(reranker_docs)
    state["reranked_chunk_ids"] = chunk_ids(reranker_docs)
    scored_docs = list(reranker_docs)  # 截断前的完整候选（权威条目保底用）
    reranker_docs = dyn_limit_reranker_docs(reranker_docs)
    reranker_docs = ensure_evolution_docs(reranker_docs, scored_docs)
    if use_manual_ranking:
        reranker_docs = keep_retrieval_agreement(state, reranker_docs, scored_docs, local_limit)
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
