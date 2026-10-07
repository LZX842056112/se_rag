"""对外引用（citation）构造：读链路落库、历史回显与实时 SSE 共用同一实现。

历史问题：API 层 ``_build_citations`` 与 ``answer_service.backfill_evolution_outputs``
各写一份相同的引用构造逻辑，容易出现「历史回显与实时返回不一致」。

第二轮问题：联网结果参与重排并可能进入最终上下文，但引用只覆盖 ``kb`` / ``evolution``，
于是「答案靠联网片段答出来」时界面显示「无引用」，用户无法判断答案出处。
"""
from __future__ import annotations

from app.evolution.schema import CitationModel

# 只接受 http/https：避免把 javascript:/data: 之类写进前端 <a href>
_ALLOWED_URL_PREFIXES = ("http://", "https://")


def split_cited(reranked_docs: list | None) -> tuple[list, list, list]:
    """从重排结果里拆出「本地引用 id / 自进化 id / 联网文档」。

    联网文档带 ``type="web"`` 且 ``chunk_id`` 为空，不属于知识库召回，单独返回。
    """
    cited: list = []
    evolution_ids: list = []
    web_docs: list = []
    for doc in reranked_docs or []:
        if not isinstance(doc, dict):
            continue
        if doc.get("type") == "web":
            web_docs.append(doc)
            continue
        chunk_id = doc.get("chunk_id")
        if chunk_id is None:
            continue
        cited.append(chunk_id)
        if doc.get("source") == "evolution":
            evolution_ids.append(chunk_id)
    return cited, evolution_ids, web_docs


def build_citations(
    cited_ids: list | None,
    evolution_ids: list | None = None,
    web_docs: list | None = None,
    doc_meta: dict[str, dict] | None = None,
) -> list[dict]:
    """按「本地引用 id + 自进化 id + 联网文档」构造引用列表（去重保序）。

    :param doc_meta: 可选的 ``{chunk_id: 命中文档}``，用于补充页码与章节面包屑（引用溯源）
    """
    evolution_set = {str(i) for i in (evolution_ids or [])}
    meta_map = doc_meta or {}
    citations: list[dict] = []
    seen: set[str] = set()
    for cid in cited_ids or []:
        if cid is None:
            continue
        key = str(cid)
        if key in seen:
            continue
        seen.add(key)
        meta = meta_map.get(key) or {}
        heading = str(meta.get("heading_path") or meta.get("title") or "").strip()
        page = meta.get("page")
        citations.append(CitationModel(
            faq_id=key,
            source="evolution" if key in evolution_set else "kb",
            page=page if isinstance(page, int) and page > 0 else None,
            heading=heading,
        ).model_dump())
    for doc in web_docs or []:
        url = str(doc.get("url") or "").strip()
        if not url.startswith(_ALLOWED_URL_PREFIXES) or url in seen:
            continue
        seen.add(url)
        citations.append(CitationModel(
            faq_id=url,
            source="web",
            title=str(doc.get("title") or "").strip(),
            score=float(doc.get("score") or 0.0),
        ).model_dump())
    return citations


def build_doc_meta(reranked_docs: list | None) -> dict[str, dict]:
    """把重排结果整理成 ``{chunk_id: 文档}``，供引用补充页码与章节信息。"""
    meta: dict[str, dict] = {}
    for doc in reranked_docs or []:
        if not isinstance(doc, dict):
            continue
        chunk_id = doc.get("chunk_id")
        if chunk_id is None:
            continue
        meta.setdefault(str(chunk_id), doc)
    return meta


def citations_from_reranked_docs(reranked_docs: list | None) -> list[dict]:
    """一步到位：由 ``reranked_docs`` 直接构造引用列表（读链路三处共用）。"""
    cited, evolution_ids, web_docs = split_cited(reranked_docs)
    return build_citations(cited, evolution_ids, web_docs, build_doc_meta(reranked_docs))
