"""自进化调度器：定时驱动「扫描未解决反馈 → 缺口分级 → 候选生成」。

复用生产函数 ``scan_unresolved_feedbacks`` / ``generate_candidate``，只做编排与兜底；
幂等由会话级去重 + 问题级去重保证。

注意：按单 worker 部署设计（任务进程内唯一）；多 worker 需另加分布式锁。
"""
from __future__ import annotations

import asyncio

from app.evolution.candidate.generator import generate_candidate
from app.evolution.gap.detector import scan_unresolved_feedbacks
from app.rag.query.embedding_search_service import search_by_milvus
from app.shared.config import settings
from app.shared.runtime.logger import logger


def _build_context(query: str) -> list[dict[str, str]]:
    """对缺口查询做一次轻量全库检索取参考片段；零命中/异常一律回退空（由 LLM 据问题提炼）。"""
    try:
        if not query:
            return []
        hits = search_by_milvus(item_names=[], rewritten_query=query) or []
        docs: list[dict[str, str]] = []
        for hit in hits:
            entity = hit.get("entity", {}) if isinstance(hit, dict) else {}
            content = entity.get("content") or ""
            if content:
                docs.append({"text": str(content)})
        return docs
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"候选生成上下文检索失败，回退空：{exc}")
        return []


def run_scan_and_generate_once() -> dict[str, int]:
    """单轮调度：扫描未解决反馈，对 strong 缺口生成候选；任何异常不外抛。"""
    result = {"scanned": 0, "generated": 0, "skipped": 0, "failed": 0}
    if not settings.evolution.enabled:
        return result
    try:
        gaps = scan_unresolved_feedbacks(batch=settings.evolution.scan_batch)
        result["scanned"] = len(gaps)
        for gap in gaps:
            if gap.status != "candidate":
                result["skipped"] += 1
                continue
            context = _build_context(gap.transcript_slice or gap.session_id) \
                if settings.evolution.gen_context_enabled else []
            try:
                candidate = generate_candidate(gap.document(), context)
            except Exception as exc:  # noqa: BLE001 - 单个缺口失败不影响本轮其余缺口
                logger.warning(f"候选生成异常：{exc}")
                result["failed"] += 1
                continue
            if candidate is None:
                result["failed"] += 1
            else:
                result["generated"] += 1
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"调度单轮执行失败：{exc}")
    return result


async def evolution_scheduler_loop(interval_seconds: float) -> None:
    """后台循环：按间隔执行单轮扫描+生成；被取消时正常结束。"""
    while True:
        try:
            result = await asyncio.to_thread(run_scan_and_generate_once)
            logger.info(
                f"调度执行完成：scanned={result['scanned']} generated={result['generated']} "
                f"skipped={result['skipped']} failed={result['failed']}"
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"调度循环异常：{exc}")
        await asyncio.sleep(interval_seconds)
