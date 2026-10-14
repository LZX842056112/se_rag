"""同步重试工具：对有界次数的瞬时故障做退避重试。

设计取舍：项目既有重试均为手写循环（``pdf_parse_service`` 的轮询、
``evolution/candidate/generator`` 的 LLM 提炼重试），此处沿用同一风格，
不引入 ``tenacity``，避免多一套日志与配置口径。

默认重试 ``OSError``：它同时覆盖内建的 ``TimeoutError`` / ``ConnectionError``，
以及 ``requests`` 的 ``RequestException``（后者继承 ``IOError`` 即 ``OSError``）。
因此调用方只需在拿到响应后把**业务错误**转成非 ``OSError`` 异常（如 ``RuntimeError``），
4xx 之类的确定性失败就不会被误重试。
"""
from __future__ import annotations

import time
from typing import Any, Callable, Sequence

from app.shared.runtime.logger import logger

# 默认重试的异常类型（见模块 docstring 的说明）
DEFAULT_RETRY_ON: tuple[type[BaseException], ...] = (OSError,)


def retry_call(
    func: Callable[[], Any],
    *,
    attempts: int = 3,
    backoff: float = 1.0,
    retry_on: Sequence[type[BaseException]] | None = None,
    on_error: Callable[[BaseException, int], None] | None = None,
    label: str = "操作",
) -> Any:
    """执行 ``func``，命中 ``retry_on`` 时退避重试，最多 ``attempts`` 次。

    :param attempts: 总尝试次数（含首次）；小于 1 时按 1 处理
    :param backoff: 退避基准秒数，第 n 次失败后等待 ``backoff * n``
    :param retry_on: 需要重试的异常类型；默认 ``OSError``
    :param on_error: 每次失败后的回调 ``(exc, attempt)``，便于调用方记录上下文
    :param label: 日志中的操作名
    """
    retry_types = tuple(retry_on) if retry_on else DEFAULT_RETRY_ON
    total = max(1, int(attempts))
    last_exc: BaseException | None = None

    for attempt in range(1, total + 1):
        try:
            return func()
        except retry_types as exc:
            last_exc = exc
            if attempt >= total:
                break
            if on_error is not None:
                on_error(exc, attempt)
            logger.warning(f"{label}第 {attempt} 次失败（稍后重试）：{exc}")
            if backoff > 0:
                time.sleep(backoff * attempt)

    assert last_exc is not None  # 循环必然至少捕获一次异常才会走到这里
    raise last_exc
