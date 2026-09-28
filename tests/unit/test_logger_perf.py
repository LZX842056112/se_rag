"""日志热路径回归：位置修正不得使用 inspect.stack()（全栈源码解析，开销极高）。"""
from __future__ import annotations

import inspect
import io
import time

from loguru import logger as loguru_logger

from app.shared.runtime import logger as logger_module


def test_position_fix_does_not_import_inspect():
    """结构性回归：模块不应再依赖 inspect（旧实现每条日志都调用 inspect.stack()）。"""
    assert "inspect" not in vars(logger_module)
    assert callable(logger_module._fix_log_position)


def _bench_old_style(records: int) -> float:
    """模拟旧实现：每条日志都执行 inspect.stack() 全栈遍历。"""
    sink = io.StringIO()
    test_logger = loguru_logger.bind()

    def old_patch(record):
        for frame in inspect.stack():
            if "_logger.py" in frame.filename or frame.function == "_log" or "logger.py" in frame.filename:
                continue
            record.update(name=frame.filename.split("\\")[-1], function=frame.function, line=frame.lineno)
            break

    patched = test_logger.patch(old_patch)
    handle = patched.add(sink, level="INFO", format="{message}")
    start = time.perf_counter()
    for index in range(records):
        patched.info(f"bench-old-{index}")
    elapsed = time.perf_counter() - start
    patched.remove(handle)
    return elapsed


def _bench_new_style(records: int) -> float:
    """当前实现：sys._getframe 逐层回溯。"""
    sink = io.StringIO()
    patched = loguru_logger.patch(logger_module._fix_log_position)
    handle = patched.add(sink, level="INFO", format="{message}")
    start = time.perf_counter()
    for index in range(records):
        patched.info(f"bench-new-{index}")
    elapsed = time.perf_counter() - start
    patched.remove(handle)
    return elapsed


def test_position_patch_is_much_faster_than_inspect_stack():
    """当前实现应显著快于 inspect.stack() 版本（保守断言 2 倍以上）。"""
    records = 300
    old_elapsed = _bench_old_style(records)
    new_elapsed = _bench_new_style(records)
    assert new_elapsed < old_elapsed, (
        f"位置修正未变快：old={old_elapsed:.4f}s new={new_elapsed:.4f}s"
    )
