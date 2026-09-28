"""真机端到端：导入 → 查询 →（开启自进化时）反馈闭环 → 清理测试数据。

运行方式（需要真实 Milvus / MongoDB / MinIO / 模型服务，且会调用大模型产生费用）::

    E2E_ENABLED=1 pytest -m e2e tests/e2e -s

测试数据全部带 ``e2e_`` 前缀，结束后按 ``file_title`` / ``evo_doc_id`` / ``session_id`` 清理。
"""
from __future__ import annotations

import asyncio
import faulthandler
import os
import time
import uuid
from pathlib import Path

import pytest

from app.evolution.feedback.collector import record_feedback
from app.evolution.index.update import deactivate
from app.evolution.models import FeedbackEvent
from app.evolution.repositories import evolution_repo
from app.evolution.scheduler import run_scan_and_generate_once
from app.rag.import_.pipeline import invoke_import_graph
from app.rag.query.pipeline import invoke_query_graph
from app.shared.clients.milvus_gateway import milvus_gateway
from app.shared.config import settings
from app.shared.runtime.logger import logger
from app.shared.utils.sse_broker import drop_channel, stream_events
from app.shared.utils.task_state import get_task_status

pytestmark = pytest.mark.e2e

# 排查用：E2E_DEBUG=1 时若用例卡住超过 600 秒，打印所有线程栈并退出（默认关闭）
if os.getenv("E2E_DEBUG") == "1":
    faulthandler.dump_traceback_later(600, exit=True)

_SAMPLE_MD = """# E2E 测试机型充说明

## 电源参数

E2E 测试机型的电源适配器输入为 100-240V，工作温度范围为 0~40 摄氏度。

## 维护提示

清洁 E2E 测试机型前请先断开电源，并使用干燥软布擦拭外壳。
"""


class _OnlineRequest:
    """最小的在线请求替身（用于消费 SSE 生成器）。"""

    async def is_disconnected(self) -> bool:
        return False


async def _drain_stream(session_id: str, max_events: int = 40) -> list[str]:
    """读取 SSE 直到收到 close 或达到事件上限。"""
    collected: list[str] = []
    generator = stream_events(session_id, _OnlineRequest())
    try:
        while len(collected) < max_events:
            chunk = await asyncio.wait_for(generator.__anext__(), timeout=120)
            collected.append(chunk)
            if "event: close" in chunk:
                break
    finally:
        await generator.aclose()
    return collected


def _query_chunks(client, filter_expr: str, output_fields: list[str],
                  expected_count: int = 1, retry_times: int = 10) -> list[dict]:
    """查询刚写入的切片（Milvus 写入后短暂可见性延迟，故做有限重试）。"""
    rows: list[dict] = []
    for _ in range(retry_times):
        rows = client.query(
            collection_name=milvus_gateway.chunk_collection_name,
            filter=filter_expr,
            output_fields=output_fields,
        )
        if len(rows) >= expected_count:
            return rows
        time.sleep(0.5)
    return rows


@pytest.fixture
def sample_doc(tmp_path: Path):
    """写一份临时 markdown 文档，返回 (file_title, 路径) 并保证测试后清理。"""
    file_title = f"e2e_sample_{uuid.uuid4().hex[:8]}"
    md_path = tmp_path / f"{file_title}.md"
    md_path.write_text(_SAMPLE_MD, encoding="utf-8")
    yield file_title, md_path
    _cleanup_milvus(file_title)


def _delete_and_verify(collection: str, filter_expr: str, *, retries: int = 3,
                       wait_seconds: float = 2.0) -> None:
    """删除后按「删除 → 等待 → 复核」确认 Milvus 真的清空。

    Milvus 删除存在短暂可见性延迟：删完立即查询可能仍读到旧数据，此前正因如此，
    跑批在 ``kb_item_names`` 留下了 ``e2e_sample_*`` 残留。清理必须复核，残留要显式报错
    而不是静默放过。
    """
    client = milvus_gateway.milvus_client
    if client is None:
        return
    remaining = -1
    for attempt in range(retries):
        try:
            if client.has_collection(collection_name=collection):
                client.delete(collection_name=collection, filter=filter_expr)
        except Exception as exc:  # noqa: BLE001 - 记录后继续重试
            logger.warning(f"清理 {collection} 失败（第 {attempt + 1} 次）：{exc}")
        time.sleep(wait_seconds)
        try:
            if client.has_collection(collection_name=collection):
                remaining = len(client.query(collection_name=collection, filter=filter_expr,
                                             output_fields=["file_title"]))
            else:
                remaining = 0
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"复核 {collection} 失败：{exc}")
            remaining = -1
        if remaining == 0:
            return
    raise AssertionError(f"测试数据清理后仍有残留：collection={collection}, filter={filter_expr}, remaining={remaining}")


def _cleanup_milvus(file_title: str) -> None:
    """按 file_title 清理测试产生的向量数据（chunks / item_name），并复核清空。"""
    from app.shared.clients.milvus_gateway import eq_expr

    filter_expr = eq_expr("file_title", file_title)
    for collection in (milvus_gateway.chunk_collection_name, milvus_gateway.item_name_collection_name):
        _delete_and_verify(collection, filter_expr)


def _cleanup_evolution_item(evo_doc_id: str) -> None:
    """下架进化条目并复核（同样是删除 + 等待 + 复核）。"""
    from app.shared.clients.milvus_gateway import eq_expr

    deactivate(evo_doc_id)
    _delete_and_verify(milvus_gateway.evolution_collection_name, eq_expr("evo_doc_id", evo_doc_id))


def test_import_query_and_evolution_flow(require_e2e, sample_doc):
    file_title, md_path = sample_doc
    task_id = f"e2e_task_{uuid.uuid4().hex[:8]}"

    # ---------- 1. 导入：真实走 切分 → 主体识别 → 向量化 → Milvus ----------
    invoke_import_graph(
        task_id=task_id,
        local_file_path=str(md_path),
        local_dir=str(md_path.parent),
    )
    assert get_task_status(task_id) == "completed", "导入任务未完成"

    client = milvus_gateway.milvus_client
    assert client is not None, "Milvus 客户端不可用"
    from app.shared.clients.milvus_gateway import eq_expr

    rows = _query_chunks(client, eq_expr("file_title", file_title), ["chunk_id", "item_name", "content"])
    assert rows, "知识库中未查询到本次导入的切片"
    item_name = rows[0]["item_name"]
    assert item_name, "导入结果缺少主体名"

    # ---------- 2. 查询：真实走 主体确认 → 多路召回 → 融合重排 → 作答 ----------
    session_id = f"e2e_sess_{uuid.uuid4().hex[:8]}"
    drop_channel(session_id)
    state = invoke_query_graph(
        session_id=session_id,
        original_query=f"{item_name} 的电源适配器工作温度范围是多少？",
        is_stream=True,
    )
    assert state is not None, "查询流程执行失败"
    assert state.get("answer"), "未生成答案"

    chunks = asyncio.run(_drain_stream(session_id))
    joined = "".join(chunks)
    assert "event: ready" in joined
    assert "event: final" in joined
    assert "groundedness" in joined and "citations" in joined
    drop_channel(session_id)

    # ---------- 3. 自进化闭环（需 EVOLUTION_ENABLED=true） ----------
    if not settings.evolution.enabled:
        pytest.skip("EVOLUTION_ENABLED=false，跳过自进化闭环校验")

    record_feedback(FeedbackEvent(
        session_id=session_id,
        query=f"e2e_{uuid.uuid4().hex[:6]} 的保修期是多少？",
        rewritten_query="保修期",
        item_names=[item_name],
        adopt=False,
        thumbs=-1,
        source="kb",
    ))
    result = run_scan_and_generate_once()
    assert result["scanned"] >= 1, "缺口扫描未消费到测试反馈"

    drafts = list(evolution_repo.k_candidates.find({"item_names": item_name, "status": "draft"}))
    if drafts:
        from app.evolution.approval import service as approval_service

        candidate_id = str(drafts[0]["_id"])
        assert approval_service.approve(candidate_id), "候选审批通过失败"
        approved = evolution_repo.k_candidates.find_one({"_id": drafts[0]["_id"]})
        evo_doc_id = approved.get("evo_doc_id")
        assert evo_doc_id, "审批后未生成 evo_doc_id"
        # 清理：下架本次审批产生的进化条目与候选记录（下架后复核确实已移出检索）
        _cleanup_evolution_item(evo_doc_id)
        evolution_repo.k_candidates.delete_one({"_id": drafts[0]["_id"]})

    # ---------- 4. 清理测试会话与缺口 ----------
    evolution_repo.fb_events.delete_many({"session_id": session_id})
    evolution_repo.k_gaps.delete_many({"session_id": session_id})
