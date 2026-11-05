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

# 任务整体状态枚举：与前端展示联动，node 的 done/running 是进度明细，状态是粗粒度汇总
TASK_STATUS_PENDING = "pending"          # 排队中（已提交未开始）
TASK_STATUS_PROCESSING = "processing"    # 处理中（图正在跑）
TASK_STATUS_COMPLETED = "completed"      # 已完成
TASK_STATUS_FAILED = "failed"            # 失败（配合 error_code / message 看分类）

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
    "node_intent_route": "意图识别",
    "node_chitchat_answer": "闲聊作答",
    "node_answer_output": "生成答案",
    "node_rerank": "重排序",
    "node_parent_expand": "父块回溯",
    "node_rrf": "倒排融合",
    "node_web_search_mcp": "网络搜索",
    "node_search_embedding": "切片搜索",
    "node_search_embedding_hyde": "切片搜索(假设性文档)",
}


@dataclass
class _TaskRecord:
    """单个任务的进度快照。"""

    running: List[str] = field(default_factory=list)  # 进行中的节点名列表（按节点执行顺序追加）
    done: List[str] = field(default_factory=list)  # 已完成的节点名列表（完成后从 running 摘除并入此列）
    status: str = ""  # 任务整体状态，取值见 TASK_STATUS_* 常量
    # 失败分类：机器码 + 中文提示（成功任务留空）
    error_code: str = ""  # 机器可读的错误码（定位用稳定标识）
    message: str = ""  # 面向用户的中文错误提示
    # 本轮提问原文：页面刷新后用户消息可能还没落库，前端用快照补出提问气泡
    query: str = ""  # 本轮提问原文（仅查询链路使用）
    turn_id: str = ""  # 本轮会话轮次 ID（关联多轮对话用）
    updated_at: float = field(default_factory=time.time)  # 最近一次访问/变更时间，用于 TTL 超时回收判定


# 进程内任务状态表 + 全局锁：task_id -> 快照记录；所有读写走锁保证图线程池并发安全
_tasks: Dict[str, _TaskRecord] = {}
_lock = threading.Lock()


def _prune_locked(now: float) -> None:
    """回收超期的任务记录（调用方需持锁）。"""
    ttl = settings.runtime.task_state_ttl_seconds
    # 找出超过 TTL 未更新的记录（updated_at 在每次访问时刷新，活跃任务不会被误回收）
    stale = [task_id for task_id, record in _tasks.items() if now - record.updated_at > ttl]
    for task_id in stale:
        _tasks.pop(task_id, None)


def _get_locked(task_id: str) -> _TaskRecord:
    """取（或创建）任务记录（调用方需持锁）。"""
    record = _tasks.get(task_id)
    if record is None:
        # 首次访问：惰性创建空记录一并入表，随后所有写操作都在该记录上累积
        record = _TaskRecord()
        _tasks[task_id] = record
    record.updated_at = time.time()  # 刷新时间戳，用于 TTL 存活判断
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
        # 同节点重复执行（如循环/重试）时只保留一个，避免列表重复
        if node_name not in record.running:
            record.running.append(node_name)
        # 流式场景：状态变更即时推给前端
        if is_stream:
            _push_progress_locked(task_id, record)


def add_done_task(task_id: str, node_name: str, is_stream: bool = False) -> None:
    """登记「已完成」的节点，并从运行列表移除同名节点。"""
    with _lock:
        _prune_locked(time.time())
        record = _get_locked(task_id)
        record.running = [n for n in record.running if n != node_name]  # 完成即从「进行中」摘除
        if node_name not in record.done:
            record.done.append(node_name)
        if is_stream:
            _push_progress_locked(task_id, record)


def get_task_status(task_id: str) -> str:
    """获取任务整体状态；未登记时返回空字符串。"""
    with _lock:
        record = _tasks.get(task_id)
        return record.status if record else ""


def set_task_query(task_id: str, query: str) -> None:
    """登记本轮提问原文（查询开始时调用）。

    用途：页面刷新后，用户消息可能还没写进会话历史（它由主体确认节点落库），
    前端据快照里的原文补出提问气泡，避免「只剩等待中的回答气泡」。
    """
    with _lock:
        _prune_locked(time.time())
        record = _get_locked(task_id)
        record.query = query or ""


def set_task_turn(task_id: str, turn_id: str) -> None:
    with _lock:
        _get_locked(task_id).turn_id = turn_id


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


def set_task_error(task_id: str, code: str, message: str = "") -> None:
    """登记任务的失败分类（机器码 + 中文提示）。"""
    with _lock:
        _prune_locked(time.time())
        record = _get_locked(task_id)
        record.error_code = code or ""
        record.message = message or ""


def get_task_error(task_id: str) -> tuple[str, str]:
    """读取任务的失败分类；未登记时返回空串。"""
    with _lock:
        record = _tasks.get(task_id)
        if record is None:
            return "", ""
        return record.error_code, record.message


def snapshot(task_id: str) -> dict:
    """返回任务状态的只读快照（用于落盘与「刷新后重建进度」，避免在锁内做 IO）。

    读取前先回收超期记录：否则服务重启前遗留的 processing 记录会被当成
    「仍在跑」，前端会一直显示等待中。
    """
    with _lock:
        _prune_locked(time.time())
        record = _tasks.get(task_id)
        if record is None:
            return {}
        return {
            "task_id": task_id,
            "status": record.status,
            "done_list": [_to_cn(n) for n in record.done],
            "running_list": [_to_cn(n) for n in record.running],
            "error_code": record.error_code,
            "message": record.message,
            "query": record.query,
            "turn_id": record.turn_id,
        }


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