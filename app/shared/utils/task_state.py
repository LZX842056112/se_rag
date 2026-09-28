"""
图执行进度的进程内状态（单 worker 部署前提）。

相对旧实现的改进：
1. 加线程锁：查询/导入的图都跑在线程池中，旧实现的多线程读写无保护；
2. TTL 回收：旧实现的字典只增不减（导入侧从不清理）会长期占用内存；
3. 删除从未被使用的任务结果存储（``_tasks_result``）。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from functools import wraps
from typing import Dict, List

from app.shared.config import settings
from app.shared.utils.sse_broker import SSEEvent, publish

TASK_STATUS_PENDING = "pending"
TASK_STATUS_PROCESSING = "processing"
TASK_STATUS_COMPLETED = "completed"
TASK_STATUS_FAILED = "failed"

# 节点名 -> 中文展示名（前端展示用；key 必须与 LangGraph 节点名一致）
_NODE_NAME_TO_CN: Dict[str, str] = {
    "upload_file": "开始上传文件",
    "node_entry": "检查文件",
    "node_pdf_to_md": "PDF转Markdown",
    "node_md_img": "Markdown图片处理",
    "node_item_name_recognition": "主体名称识别",
    "node_document_split": "文档切分",
    "node_bge_embedding": "向量生成",
    "node_import_milvus": "导入向量库",
    "__end__": "处理完成",
    "END": "处理完成",
    "node_item_name_confirm": "确认问题产品",
    "node_answer_output": "生成答案",
    "node_rerank": "重排序",
    "node_rrf": "倒排融合",
    "node_web_search_mcp": "网络搜索",
    "node_search_embedding": "切片搜索",
    "node_search_embedding_hyde": "切片搜索(假设性文档)",
}


@dataclass
class _TaskRecord:
    """单个任务的进度快照。"""

    running: List[str] = field(default_factory=list)
    done: List[str] = field(default_factory=list)
    status: str = ""
    updated_at: float = field(default_factory=time.time)


_tasks: Dict[str, _TaskRecord] = {}
_lock = threading.Lock()


def _prune_locked(now: float) -> None:
    """回收超期的任务记录（调用方需持锁）。"""
    ttl = settings.runtime.task_state_ttl_seconds
    stale = [task_id for task_id, record in _tasks.items() if now - record.updated_at > ttl]
    for task_id in stale:
        _tasks.pop(task_id, None)


def _get_locked(task_id: str) -> _TaskRecord:
    """取（或创建）任务记录（调用方需持锁）。"""
    record = _tasks.get(task_id)
    if record is None:
        record = _TaskRecord()
        _tasks[task_id] = record
    record.updated_at = time.time()
    return record


def _to_cn(node_name: str) -> str:
    """节点名转中文展示名；无映射时返回原名。"""
    return _NODE_NAME_TO_CN.get(node_name, node_name)


def _push_progress_locked(task_id: str, record: _TaskRecord) -> None:
    """把进度推送到 SSE（调用方需持锁，publish 内部会再取锁，故此处先取快照）。"""
    publish(task_id, SSEEvent.PROGRESS, {
        "status": record.status,
        "done_list": [_to_cn(n) for n in record.done],
        "running_list": [_to_cn(n) for n in record.running],
    })


def add_running_task(task_id: str, node_name: str, is_stream: bool = False) -> None:
    """登记「正在运行」的节点（去重）。"""
    with _lock:
        _prune_locked(time.time())
        record = _get_locked(task_id)
        if node_name not in record.running:
            record.running.append(node_name)
        if is_stream:
            _push_progress_locked(task_id, record)


def add_done_task(task_id: str, node_name: str, is_stream: bool = False) -> None:
    """登记「已完成」的节点，并从运行列表移除同名节点。"""
    with _lock:
        _prune_locked(time.time())
        record = _get_locked(task_id)
        record.running = [n for n in record.running if n != node_name]
        if node_name not in record.done:
            record.done.append(node_name)
        if is_stream:
            _push_progress_locked(task_id, record)


def get_task_status(task_id: str) -> str:
    """获取任务整体状态；未登记时返回空字符串。"""
    with _lock:
        record = _tasks.get(task_id)
        return record.status if record else ""


def update_task_status(task_id: str, status_name: str, push_queue: bool = False) -> None:
    """更新任务整体状态，可选推送一次进度事件。"""
    with _lock:
        _prune_locked(time.time())
        record = _get_locked(task_id)
        record.status = status_name
        if push_queue:
            _push_progress_locked(task_id, record)


def get_done_task_list(task_id: str) -> List[str]:
    """获取已完成节点列表（中文展示）。"""
    with _lock:
        record = _tasks.get(task_id)
        return [_to_cn(n) for n in record.done] if record else []


def get_running_task_list(task_id: str) -> List[str]:
    """获取进行中节点列表（中文展示）。"""
    with _lock:
        record = _tasks.get(task_id)
        return [_to_cn(n) for n in record.running] if record else []


def clear_task(task_id: str) -> None:
    """清理任务进度记录（会话/任务结束时调用）。"""
    with _lock:
        _tasks.pop(task_id, None)


def task_count() -> int:
    """当前在册任务数（供测试与运维观测）。"""
    with _lock:
        return len(_tasks)


def track_node_task(node_name: str, id_key: str = "session_id", stream_key: str = "is_stream"):
    """装饰器：在图节点执行前后自动登记 running / done。

    用法::

        @track_node_task("node_search_embedding")            # 查询图：state 含 session_id / is_stream
        @track_node_task("node_entry", id_key="task_id")      # 导入图：state 含 task_id，无流式
    """
    def decorator(func):
        @wraps(func)
        def wrapper(state, *args, **kwargs):
            task_id = state.get(id_key)
            is_stream = state.get(stream_key, False)
            add_running_task(task_id, node_name, is_stream)
            try:
                return func(state, *args, **kwargs)
            finally:
                add_done_task(task_id, node_name, is_stream)
        return wrapper
    return decorator
