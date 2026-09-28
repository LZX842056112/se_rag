"""滑动窗口限速器：保护第三方 API（如视觉/对话模型）不被触发限流。"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Deque

from app.shared.runtime.logger import logger

_REQUEST_TIMES: Deque[float] = deque()
_LOCK = threading.Lock()


def apply_api_rate_limit(max_requests: int = 3000, window_seconds: int = 60) -> None:
    """滑动窗口限速：窗口内请求数达上限时阻塞等待到窗口滑出。

    :param max_requests: 窗口内允许的最大请求次数
    :param window_seconds: 滑动窗口长度（秒）
    """
    while True:
        with _LOCK:
            now = time.time()
            while _REQUEST_TIMES and now - _REQUEST_TIMES[0] >= window_seconds:
                _REQUEST_TIMES.popleft()
            if len(_REQUEST_TIMES) < max_requests:
                _REQUEST_TIMES.append(now)
                return
            sleep_duration = window_seconds - (now - _REQUEST_TIMES[0])
        if sleep_duration > 0:
            logger.debug(f"触发 API 限速，等待 {sleep_duration:.2f} 秒（窗口 {window_seconds}s / {max_requests} 次）")
            time.sleep(sleep_duration)
