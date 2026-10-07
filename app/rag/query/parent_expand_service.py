"""父块回溯（Small-to-Big）：命中子块后回填其所属章节的完整背景。

**为什么放在重排之后**：重排仍以**子块**文本打分（子块短、主题集中，打分精度高）；父块只作为
最终上下文的补充。若把父块一并喂给重排，长父块会稀释相关性分数，把精确命中的子块压下去。

**与其它来源的关系**：联网结果（``type == "web"``）与自进化条目（``source == "evolution"``）
都没有父块，按条件过滤后自动跳过；``ensure_evolution_docs`` 的保底逻辑不依赖 parent_id，不受影响。
"""
from __future__ import annotations

from app.rag.query.config import PARENT_MAX_CHARS, PARENT_TOTAL_BUDGET_CHARS
from app.shared.clients.parent_chunk_repository import parent_chunk_repository
from app.shared.runtime.logger import logger, step_log


def is_local_chunk(doc: dict) -> bool:
    """是否为「有父块可回溯」的本地知识库子块。"""
    return doc.get("type") != "web" and doc.get("source") != "evolution" and bool(doc.get("parent_id"))


@step_log("fetch_parents")
def fetch_parents(parent_ids: list[str]) -> dict[str, dict]:
    """按 ``parent_id`` 批量取回父块（单次 ``$in`` 查询）；失败返回空字典。"""
    return parent_chunk_repository.fetch_by_ids(parent_ids)


@step_log("expand_docs_with_parents")
def expand_docs_with_parents(docs: list[dict]) -> list[dict]:
    """给命中的本地子块挂上章节背景（``parent_content`` / ``parent_title``）。

    预算按重排分数降序分配：单个父块不超过 ``PARENT_MAX_CHARS``（超出从头部截断——章节开头
    通常是定义与前提，信息密度最高），合计不超过 ``PARENT_TOTAL_BUDGET_CHARS``；同一父块被
    多个子块命中时只注入一次，避免重复占用 token。
    """
    candidates = [doc for doc in docs or [] if is_local_chunk(doc)]
    if not candidates:
        return docs

    parents = fetch_parents([doc["parent_id"] for doc in candidates])
    if not parents:
        logger.warning(f"命中 {len(candidates)} 个本地子块但未取到任何父块，本次不注入章节背景")
        return docs

    remaining = PARENT_TOTAL_BUDGET_CHARS
    injected: set[str] = set()
    for doc in sorted(candidates, key=lambda item: float(item.get("score") or 0.0), reverse=True):
        parent_id = doc["parent_id"]
        if parent_id in injected:
            continue
        parent = parents.get(parent_id)
        if not parent:
            continue
        budget = min(PARENT_MAX_CHARS, remaining)
        if budget <= 0:
            break
        content = str(parent.get("content") or "").strip()
        if len(content) > budget:
            content = f"{content[:budget]}…"
        doc["parent_content"] = content
        doc["parent_title"] = parent.get("title") or ""
        injected.add(parent_id)
        remaining -= len(content)

    logger.info(f"父块回溯完成：命中本地子块={len(candidates)} 注入父块={len(injected)} 剩余预算={remaining}")
    return docs
