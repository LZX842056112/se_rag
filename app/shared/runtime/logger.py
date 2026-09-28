"""
项目日志工具（基于 loguru）。

特性：
1. 配置驱动：``LOG_*`` 环境变量控制控制台/文件输出与级别；
2. 自动路径：文件日志默认输出到 ``项目根/logs/app_YYYYMMDD.log``，按天轮转并保留；
3. 调用位置准确：穿透 loguru 内部帧与工具类自身帧，显示业务代码的真实位置；
4. 低开销：位置修正使用 ``sys._getframe`` 逐层回溯，**不再**对每条日志执行
   ``inspect.stack()``（后者会为整个调用栈解析源码行，是旧实现最主要的日志开销）；
5. 两个装饰器：``node_log``（LangGraph 节点）、``step_log``（业务步骤，默认 DEBUG）。
"""
from __future__ import annotations

import sys
import time
from functools import wraps
from pathlib import Path
from typing import Mapping

from loguru import logger as _loguru_logger

from app.shared.config.common import env_bool, env_str
from app.shared.utils.paths import PROJECT_ROOT

LOG_CONSOLE_ENABLE = env_bool("LOG_CONSOLE_ENABLE", True)
LOG_CONSOLE_LEVEL = env_str("LOG_CONSOLE_LEVEL", "INFO").upper()
LOG_FILE_ENABLE = env_bool("LOG_FILE_ENABLE", True)
LOG_FILE_LEVEL = env_str("LOG_FILE_LEVEL", "INFO").upper()
LOG_FILE_RETENTION = env_str("LOG_FILE_RETENTION", "7 days")

LOG_DIR = PROJECT_ROOT / "logs"
LOG_FILE_NAME = "app_{time:YYYYMMDD}.log"
LOG_FILE_PATH = LOG_DIR / LOG_FILE_NAME

LOG_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
    "<level>{level: <8}</level> | "
    "<cyan>{name: <20}</cyan>:<cyan>{function: <15}</cyan>:<cyan>{line: <4}</cyan> - "
    "<level>{message}</level>"
)

# 位置修正时需要跳过的框架文件（loguru 内部与工具类自身）
_INTERNAL_FILE_MARKERS = ("_logger.py", "logger.py")


def init_logger():
    """初始化全局日志配置：控制台 + 文件双通道，按 .env 开关与级别生效。"""
    _loguru_logger.remove()

    if LOG_CONSOLE_ENABLE:
        _loguru_logger.add(
            sink=sys.stdout,
            level=LOG_CONSOLE_LEVEL,
            format=LOG_FORMAT,
            colorize=True,
            enqueue=True,
        )

    if LOG_FILE_ENABLE:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        _loguru_logger.add(
            sink=LOG_FILE_PATH,
            level=LOG_FILE_LEVEL,
            format=LOG_FORMAT,
            rotation="00:00",
            retention=LOG_FILE_RETENTION,
            encoding="utf-8",
            enqueue=True,
            backtrace=True,
            diagnose=True,
        )
    return _loguru_logger


def _fix_log_position(record) -> None:
    """把日志位置改写为业务代码的真实调用点。

    使用 ``sys._getframe`` 逐层回溯（纯指针跳转，成本极低），跳过 loguru 内部帧与本
    工具类自身的帧；旧实现使用 ``inspect.stack()`` 会遍历并解析整条调用栈，是热路径
    中最主要的开销来源。
    """
    frame = sys._getframe(1)
    while frame is not None:
        filename = frame.f_code.co_filename
        if frame.f_code.co_name != "_log" and not any(m in filename for m in _INTERNAL_FILE_MARKERS):
            record.update(
                name=Path(filename).name,
                function=frame.f_code.co_name,
                line=frame.f_lineno,
            )
            return
        frame = frame.f_back


logger = init_logger().patch(_fix_log_position)


def _trace_id(state) -> str:
    """从 state 中取追踪 ID（查询用 session_id，导入用 task_id）。"""
    if isinstance(state, Mapping):
        return str(state.get("session_id") or state.get("task_id") or "-")
    return "-"


def node_log(node_name: str):
    """节点日志装饰器：记录 LangGraph 节点的开始、耗时与异常堆栈。"""
    def deco(func):
        @wraps(func)
        def wrapper(state, *args, **kwargs):
            trace_id = _trace_id(state)
            start_ts = time.time()
            logger.info(f"[{node_name}] 节点开始，追踪ID={trace_id}")
            try:
                result = func(state, *args, **kwargs)
            except Exception:
                logger.exception(f"[{node_name}] 节点异常，追踪ID={trace_id}")
                raise
            cost_ms = int((time.time() - start_ts) * 1000)
            logger.info(f"[{node_name}] 节点完成，追踪ID={trace_id}，耗时={cost_ms}ms")
            return result
        return wrapper
    return deco


def step_log(step_name: str):
    """业务步骤日志装饰器（DEBUG 级，避免每步两行 INFO 刷屏）。

    异常仍以 exception 级别记录完整堆栈，不吞异常。
    """
    def deco(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            start_ts = time.time()
            logger.debug(f"[{step_name}] 步骤开始")
            try:
                result = func(*args, **kwargs)
            except Exception:
                logger.exception(f"[{step_name}] 步骤异常")
                raise
            cost_ms = int((time.time() - start_ts) * 1000)
            logger.debug(f"[{step_name}] 步骤完成，耗时={cost_ms}ms")
            return result
        return wrapper
    return deco
