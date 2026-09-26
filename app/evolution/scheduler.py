"""
自进化调度器：定时驱动「扫描未解决反馈 → 缺口分级 → 候选生成」。
复用生产函数 scan_unresolved_feedbacks / generate_candidate，仅做编排与兜底；
幂等由现有去重保证（会话级 _de_duplication_key + faq_question 级 _exists_question）。
注意：按单 worker 部署设计，调度任务全进程唯一；多 worker 需另加分布式锁。
"""
from __future__ import annotations

import asyncio
from typing import Any

from app.evolution.candidate.generator import generate_candidate
from app.evolution.config import evolution_config
from app.evolution.gap.detector import scan_unresolved_feedbacks
from app.rag.query.embedding_search_service import search_by_milvus
from app.shared.runtime.logger import logger


def _build_context(query: str) -> list[dict[str, str]]:
    """对缺口查询做一次轻量检索取参考片段；零命中/异常一律回退空（LLM 据问题提炼）。"""
    try:
        if not query:
            return []
        hits = search_by_milvus(item_names=[], rewritten_query=query) or []
        docs: list[dict[str, str]] = []
        for h in hits:
            entity = h.get("entity", {}) if isinstance(h, dict) else {}
            text = entity.get("content") or ""
            if text:
                docs.append({"text": str(text)})
        return docs
    except Exception as e:
        logger.warning(f"候选生成上下文检索失败，回退空: {e}")
        return []


def run_scan_and_generate_once() -> dict[str, int]:
    """单轮调度：扫未解决反馈，对 strong 缺口生成候选。任何异常不外抛。"""
    result = {"scanned": 0, "generated": 0, "skipped": 0, "failed": 0}
    if not getattr(evolution_config, "enabled", False):
        return result
    try:
        gaps = scan_unresolved_feedbacks(batch=evolution_config.scan_batch)
        result["scanned"] = len(gaps)
        for gap in gaps:
            if gap.status != "candidate":
                result["skipped"] += 1
                continue
            context = _build_context(gap.transcript_slice or gap.session_id) \
                if evolution_config.gen_context_enabled else []
            try:
                candidate = generate_candidate(gap.document(), context)
            except Exception as e:
                logger.warning(f"候选生成异常: {e}")
                result["failed"] += 1
                continue
            if candidate is None:
                result["failed"] += 1
            else:
                result["generated"] += 1
    except Exception as e:
        logger.exception(f"调度单轮执行失败: {e}")
    return result


async def evolution_scheduler_loop(interval_seconds: float) -> None:
    """后台循环：间隔执行单轮扫描+生成。被取消时正常结束。"""
    while True:
        try:
            result = await asyncio.to_thread(run_scan_and_generate_once)
            logger.info(
                f"调度执行完成: scanned={result['scanned']} generated={result['generated']} "
                f"skipped={result['skipped']} failed={result['failed']}"
            )
        except Exception as e:
            logger.exception(f"调度循环异常: {e}")
        await asyncio.sleep(interval_seconds)
