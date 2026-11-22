"""在已确认的产品范围内补充正文和章节关键词召回。"""
from __future__ import annotations

from collections import Counter
from itertools import zip_longest
from math import log1p
import re
from unicodedata import normalize

from app.rag.item_name.catalog import expand_item_names
from app.rag.query.chunk_search import CHUNK_OUTPUT_FIELDS, product_chunk_filter, to_chunks
from app.shared.clients.milvus_gateway import milvus_gateway
from app.shared.config import settings
from app.shared.runtime.deadline import bounded_timeout
from app.shared.runtime.logger import logger
from app.shared.runtime.query_metrics import mark_degraded
from app.shared.utils.text import normalize_item_name, product_model_tokens


KEYWORD_CANDIDATE_LIMIT = 12
_SCAN_LIMIT = 2048
_WORDS = re.compile(r"[\u3400-\u9fff]+|[a-z]+[0-9]*|[0-9]+")
_IDENTIFIER = re.compile(r"\b[a-z0-9]+(?:[/-][a-z0-9]+)+\b")
_LETTER_NUMBER = re.compile(r"[a-z]{2,}\d+[a-z0-9]*")
_MENU_PHRASE = re.compile(r"[a-z][a-z0-9]*(?:[ \t]+[a-z0-9]+)+")
_BRADMIN_ALIAS = re.compile(r"\bbraadmin\b")
_HEADING_ALIAS = re.compile(r"\(([^\s()]{2,12})\)(?=\s*(?:/|$))")
_MODEL_COMPARISON = re.compile(r"分别|各自|区别|差异|不同|相比|对比|\b(?:respectively|difference|compare|versus|vs)\b", re.I)
_MODEL_LABEL = re.compile(r"^[ \t]*[(（]([a-z0-9][a-z0-9/, \t-]*)[)）][ \t\r]*$", re.I | re.M)
_IMAGE = re.compile(r"!\[[^\]\n]*\]\([^\n)]*\)")
_CONTENTS_HEADINGS = {"目录", "contents", "table of contents"}


def _terms(text: str):
    text = _BRADMIN_ALIAS.sub("bradmin", normalize("NFKC", text).casefold())
    yield from _IDENTIFIER.findall(text)
    for phrase in _MENU_PHRASE.findall(text):
        words = phrase.split()
        for size in (2, 3):
            for start in range(len(words) - size + 1):
                yield " ".join(words[start:start + size])
    for token in _WORDS.findall(text):
        if token.isascii():
            yield token
        else:
            for size in (2, 3):
                for start in range(len(token) - size + 1):
                    yield token[start:start + size]


def _outside_model_scope(row: dict, heading: str, requested: set[str], *, require_all: bool = False) -> bool:
    name_models = product_model_tokens(str(row.get("item_name") or ""))
    selected = name_models & requested
    if len(name_models) < 2 or not selected:
        return False
    for section in heading.split(" / "):
        scope_models = product_model_tokens(section.partition("适用于")[2]) & name_models
        if not scope_models:
            continue
        matches = selected & scope_models
        if not matches or (require_all and matches != selected):
            return True
    return False


def _heading_match_length(heading: str, terms: set[str], query: str, *, match_parenthetical_aliases: bool) -> int:
    title = _BRADMIN_ALIAS.sub("bradmin", normalize("NFKC", heading).casefold())
    leaf = title.rsplit(" / ", 1)[-1].strip()
    aliases = _HEADING_ALIAS.findall(title) if match_parenthetical_aliases else []
    if _IDENTIFIER.fullmatch(leaf) or _MENU_PHRASE.fullmatch(leaf) or _LETTER_NUMBER.fullmatch(leaf):
        aliases.append(leaf)
    return max((len(alias) for alias in aliases if alias in terms and alias in query), default=0)


def matches_requested_heading(row: dict, question: str) -> bool:
    heading = str(row.get("heading_path") or "")
    if _outside_model_scope(row, heading, product_model_tokens(question)):
        return False
    leaf = normalize("NFKC", heading.rsplit(" / ", 1)[-1]).casefold().strip()
    query = normalize("NFKC", question).casefold()
    if _IDENTIFIER.fullmatch(leaf) or _MENU_PHRASE.fullmatch(leaf) or _LETTER_NUMBER.fullmatch(leaf):
        return leaf in set(_terms(query))
    return 4 <= len(leaf) <= 24 and any(
        name in query for name in (f"通过{leaf}", f"使用{leaf}", f"选择{leaf}", f"「{leaf}」", f"“{leaf}”")
    )


def _bm25(terms: set[str], texts: list[str]) -> list[float]:
    frequencies = [Counter(_terms(text)) for text in texts]
    lengths = [sum(row.values()) for row in frequencies]
    average = max(1.0, sum(lengths) / len(texts))
    occurrences = Counter(term for row in frequencies for term in row.keys() & terms)
    idf = {term: log1p((len(texts) - count + .5) / (count + .5)) for term, count in occurrences.items()}
    scores = []
    for row, length in zip(frequencies, lengths):
        length_factor = 1.2 * (.25 + .75 * length / average)
        scores.append(sum(idf[term] * 2.2 * row[term] / (row[term] + length_factor)
                          for term in sorted(row.keys() & terms)))
    return scores


def rank_manual_keywords(
    rows: list[dict], question: str, *, limit: int = KEYWORD_CANDIDATE_LIMIT, match_parenthetical_aliases: bool = True,
) -> list[dict]:
    """交替取正文和标题排名靠前的切片。标题内的模式别名也可参与匹配。"""
    terms = set(_terms(question))
    if not rows or not terms or limit <= 0:
        return []

    headings = [str(row.get("heading_path") or "") for row in rows]
    body_scores = _bm25(terms, [str(row.get("content") or "") for row in rows])
    title_scores = _bm25(terms, headings)
    query = _BRADMIN_ALIAS.sub("bradmin", normalize("NFKC", question).casefold())
    aliases = [_heading_match_length(heading, terms, query, match_parenthetical_aliases=match_parenthetical_aliases)
               for heading in headings]
    requested = product_model_tokens(question)
    outside = [_outside_model_scope(row, heading, requested) for row, heading in zip(rows, headings)]

    indexes = range(len(rows))
    by_body = sorted(indexes, key=lambda i: (outside[i], -aliases[i], -body_scores[i], str(rows[i].get("chunk_id"))))
    by_title = sorted(indexes, key=lambda i: (outside[i], -aliases[i], -(body_scores[i] + 1.5 * title_scores[i]),
                                             str(rows[i].get("chunk_id"))))
    ranked: list[dict] = []
    seen = set()
    for pair in zip_longest(by_body, by_title):
        for i in pair:
            if i is None or not (aliases[i] or body_scores[i] or title_scores[i]):
                continue
            identity = rows[i].get("chunk_id")
            if not identity or identity in seen:
                continue
            seen.add(identity)
            ranked.append(rows[i])
            if len(ranked) == limit:
                return ranked
    return ranked


def rank_shared_manual_fallback(
    docs: list[dict], question: str, item_names: list[str], *, limit: int,
) -> list[dict]:
    """同一手册的多个型号共用一项功能时，降级也优先选适用于所有被问型号的正文。"""
    items = {normalize_item_name(name): name for name in item_names if name}
    if len(items) != 1 or _MODEL_COMPARISON.search(question):
        return []
    item = next(iter(items.values()))
    if "/" not in item and "／" not in item:
        return []
    variants = [product_model_tokens(part) for part in normalize("NFKC", item).split("/")]
    if any(len(models) != 1 for models in variants):
        return []
    requested = set.union(*variants) & product_model_tokens(question)
    if len(requested) < 2:
        return []

    originals = {}
    rank_input = []
    for doc in docs:
        heading = doc.get("heading_path") or doc.get("title") or ""
        if not requested <= product_model_tokens(doc.get("item_name") or ""):
            continue
        if _outside_model_scope(doc, heading, requested, require_all=True):
            continue
        if any(part.strip().casefold() in _CONTENTS_HEADINGS for part in heading.split(" / ")):
            continue
        body = _IMAGE.sub(" ", doc.get("text") or "")
        labels = (product_model_tokens(label) & requested for label in _MODEL_LABEL.findall(body))
        if any(models and models != requested for models in labels):
            continue
        originals[doc["chunk_id"]] = doc
        rank_input.append(dict(doc, content=body))
    ranked = rank_manual_keywords(rank_input, question, limit=limit, match_parenthetical_aliases=False)
    return [originals[doc["chunk_id"]] for doc in ranked]


def search_manual_keywords(item_names: list[str], question: str, heading_path: str = "") -> list[dict] | None:
    """读取当前切片。主体过大或读取失败时交由向量检索处理。"""
    names = expand_item_names(item_names) if item_names else []
    if not names or not _WORDS.search(normalize("NFKC", question).casefold()):
        return None
    try:
        client = milvus_gateway.milvus_client
        if client is None:
            return None
        rows = client.query(
            collection_name=milvus_gateway.chunk_collection_name,
            filter=product_chunk_filter(names, heading_path),
            output_fields=CHUNK_OUTPUT_FIELDS,
            limit=_SCAN_LIMIT,
            timeout=bounded_timeout(min(settings.milvus.timeout_seconds, 2.0)),
        )
    except Exception as exc:
        mark_degraded("keyword_search_error")
        logger.warning(f"关键词召回失败：{exc}")
        return None
    if rows is None or len(rows) >= _SCAN_LIMIT:
        return None
    manual_rows = [row for row in rows if row.get("document_kind") in (None, "", "manual")]
    return to_chunks([{"entity": row} for row in rank_manual_keywords(manual_rows, question)])
