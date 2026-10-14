"""导入完成后的跨存储对账：只读校验 + 只记日志，绝不阻断导入。

背景：一次导入会写三处存储——子块写 Milvus、父块写 Mongo、图片写 MinIO，彼此没有事务。
写入顺序已改为「先写新、后清旧」以杜绝数据空窗，但对账仍能暴露「某一路静默少写」：
例如 Milvus 部分分片失败、Mongo 索引冲突丢行、MinIO 上传被限流跳过。

**Milvus 可见性延迟**：``query`` 对尚未 seal 的 growing segment 存在延迟，刚 ``upsert`` 完
立刻查询可能稳定读到 0 行（本仓 ``rag_eval/runner`` 已记录同一现象）。因此这里按
「先重试、第二次起 flush 后重查」等待可见，避免把延迟误报成「对账不符」。

**为什么比对 chunk_id 集合而不是条数**：按 ``file_title`` 计数时，上一次导入的旧块同样
计入总数——若旧块条数恰好等于期望值，条数比对会「假通过」，掩盖新块尚未可见的事实。
集合比对直接回答「本次产出的每个 chunk_id 是否都可查」，与旧数据无关。

口径说明：
- Milvus / Mongo 有明确的期望，做**集合/条数比较**；
- MinIO 无法从 state 推出期望图片数（Markdown 里的引用才是权威），故只输出
  **对象计数供观察**，不做判定，避免误报。
"""
from __future__ import annotations

import time

from app.shared.clients.milvus_gateway import eq_expr, milvus_gateway
from app.shared.config import settings
from app.shared.runtime.logger import logger, step_log

# 单次查询上限：按 file_title 过滤后的条数远小于该值
_RECONCILE_QUERY_LIMIT = 16384
# Milvus 写后可见性等待：重试次数与退避基准（秒）
_MILVUS_QUERY_ATTEMPTS = 5
_MILVUS_QUERY_BACKOFF_SECONDS = 0.2


def _safe_flush(client) -> None:
    """flush 让 growing segment 落盘可见；失败不致命（后续重试仍可能读到）。"""
    try:
        client.flush(collection_name=milvus_gateway.chunk_collection_name)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"对账 flush 失败（继续重试）：{exc}")


def _wait_visible_chunk_ids(file_title: str, expected_ids: set[str]) -> set[str] | None:
    """等待本次写入的 chunk_id 全部可查，返回可见集合；无法判定时返回 ``None``。

    等待条件为「期望集合被可见集合覆盖」，比「查到条数 > 0」更准确：
    后者在上一次导入的旧块仍可见时会立刻满足，从而掩盖新块不可见的事实。
    """
    client = milvus_gateway.milvus_client
    if client is None:
        logger.warning("对账：Milvus 客户端不可用，跳过子块对账")
        return None

    visible: set[str] = set()
    last_error: Exception | None = None
    for attempt in range(_MILVUS_QUERY_ATTEMPTS):
        if attempt == 1:
            _safe_flush(client)
        try:
            rows = client.query(
                collection_name=milvus_gateway.chunk_collection_name,
                filter=eq_expr("file_title", file_title),
                output_fields=["chunk_id"],
                limit=_RECONCILE_QUERY_LIMIT,
            )
        except Exception as exc:  # noqa: BLE001 - 计数失败按「未知」处理
            last_error = exc
        else:
            last_error = None
            visible = {str(row.get("chunk_id")) for row in (rows or []) if row.get("chunk_id")}
            if expected_ids <= visible:
                return visible
        if attempt < _MILVUS_QUERY_ATTEMPTS - 1:
            time.sleep(_MILVUS_QUERY_BACKOFF_SECONDS * (attempt + 1))

    if last_error is not None and not visible:
        logger.warning(f"对账：Milvus 子块查询失败：{last_error}")
        return None
    return visible


def _count_parent_chunks(doc_id: str) -> int | None:
    """统计某文档在 Mongo 的父块条数；失败返回 ``None``。

    Mongo 是强一致读，无需可见性等待。
    """
    try:
        from app.shared.clients.parent_chunk_repository import parent_chunk_repository

        return parent_chunk_repository.count_by_doc(doc_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"对账：Mongo 父块计数失败：{exc}")
        return None


def _count_images(stem: str) -> int | None:
    """统计某文档在 MinIO 的对象数（仅观察，不做判定）；失败返回 ``None``。"""
    try:
        from app.shared.clients.minio_gateway import minio_gateway

        return minio_gateway.count_images(stem)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"对账：MinIO 图片计数失败：{exc}")
        return None


@step_log("reconcile_after_import")
def reconcile_after_import(state: dict) -> dict:
    """对账一次导入的落地结果，返回差异字典（无差异为空）。

    任何异常都在内部消化，调用方无需处理。
    """
    if not settings.import_.reconcile_enabled:
        return {}

    diffs: dict[str, object] = {}
    file_title = str(state.get("file_title") or "")
    chunks = state.get("chunks") or []
    parents = state.get("parent_chunks") or []
    expected_ids = {str(chunk.get("chunk_id")) for chunk in chunks if chunk.get("chunk_id")}

    if file_title and expected_ids:
        visible = _wait_visible_chunk_ids(file_title, expected_ids)
        if visible is None:
            logger.warning(f"对账跳过：无法读取 Milvus 子块（file_title={file_title}）")
        elif len(visible) >= _RECONCILE_QUERY_LIMIT:
            # 命中查询上限，无法判断完整性；只记录观察值，不报差异
            logger.warning(
                f"对账观察：{file_title} 可见子块达到查询上限 {_RECONCILE_QUERY_LIMIT}，跳过完整性判定"
            )
        else:
            missing = expected_ids - visible
            if missing:
                diffs["milvus_chunks"] = {"expected": len(expected_ids), "missing": len(missing)}
                logger.error(
                    f"对账不符：Milvus 缺少 {len(missing)}/{len(expected_ids)} 个子块"
                    f"（file_title={file_title}；已 flush 并重试 {_MILVUS_QUERY_ATTEMPTS} 次）"
                )

    if parents:
        doc_id = str(parents[0].get("doc_id") or "")
        actual = _count_parent_chunks(doc_id) if doc_id else None
        if actual is not None and actual != len(parents):
            diffs["mongo_parents"] = {"expected": len(parents), "actual": actual}
            logger.error(
                f"对账不符：Mongo 父块 {actual} 条 != 期望 {len(parents)} 条（doc_id={doc_id}）"
            )

    if file_title:
        # 图片对象键用的是**原始** file_title 前缀：md_path 在上游已被替换为 `<stem>_new.md`，
        # 直接取它的 stem 会得到 `<stem>_new`，永远查不到对象。
        images = _count_images(file_title)
        if images is not None:
            logger.info(f"对账观察：MinIO 图片对象 {images} 个（stem={file_title}）")

    if not diffs:
        logger.info(f"对账通过：file_title={file_title}，子块 {len(expected_ids)} 条、父块 {len(parents)} 条")
    return diffs
