"""逐请求的限制与稳定的会话放量。不进行远程 tokenizer 下载。"""
from __future__ import annotations

import hashlib
import json
import math
import time


class StopRun(RuntimeError):
    """主动终止一次 Agent 运行的控制信号。

    通过带标签的 message（如 'deadline' / 'tool_budget' / 'web_budget'）向调用方
    传递「为什么停止」，方便上层日志记录与区分不同类型的预算用尽。
    """

    pass


def selected_mode(session_id, *, mode='legacy', percent=0):
    """按会话稳定分流：同样的 session_id 永远落到同一档位（agentic / legacy）。

    用 session_id 的 SHA-256 哈希取前 8 位十六进制再对 100 取模，得到一个 0~99
    的稳定「桶」。只有显式开启 agentic 且当前桶落在 ``percent`` 比例内才走 agentic，
    否则走 legacy。同一会话不因时序抖动而换档，保证放量可预期。
    """
    bucket = int(hashlib.sha256(str(session_id).encode()).hexdigest()[:8], 16) % 100
    return 'agentic' if mode == 'agentic' and bucket < percent else 'legacy'


class Budget:
    """单次 Agent 运行的资源预算：调用次数、联网次数、上下文 token、总时长。

    四类预算任一用尽都会抛出 ``StopRun`` 终止本轮运行，避免单次请求拖垮成本
    或上下文过长。``clock`` / ``tokenizer`` 可注入，便于单元测试替换真实实现。
    """

    def __init__(self, *, max_calls=4, max_web=1, max_tokens=8000, seconds=60,
                 clock=time.monotonic, tokenizer=None):
        """初始化预算参数并启动计时。

        - max_calls:   工具调用总次数上限；
        - max_web:     web_search 独立的调用次数上限（联网更贵，单独限制）；
        - max_tokens:  上下文 token 总消耗上限；
        - seconds:     本轮运行的最长时长；
        - clock:       计时函数（默认单调时钟），可注入便于测试；
        - tokenizer:   可选本地 tokenizer；不提供时按 UTF-8 字节数估算上限。
        """
        # 时长必须是有限正整数，否则 deadline 无从谈起
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError('deadline must be finite and positive')
        self.clock, self.started = clock, clock()
        self.deadline = self.started + seconds
        self.max_calls, self.max_web, self.max_tokens = max_calls, max_web, max_tokens
        self.calls = self.web_calls = self.tokens = 0
        self.seen = set()  # 已执行过的 `[工具, 参数]` 签名，用于去重防重复动作
        self.tokenizer = tokenizer
        # 记录当前 token 计量方式，便于诊断与日志
        self.counting = 'local_tokenizer' if tokenizer else 'utf8_bytes_upper_bound'

    def remaining(self):
        """当前剩余时长（秒）；已超时则抛 ``StopRun('deadline')``。"""
        remaining = self.deadline - self.clock()
        if remaining <= 0:
            raise StopRun('deadline')
        return remaining

    def take(self, tool, arguments):
        """登记一次工具调用：在调用前检查各预算门限。

        顺序：先检查总时长 → 总调用次数 → 是否重复动作 → 联网专属上限，
        任一不满足即抛对应 ``StopRun``，通过则记账（次数 / 联网次数 / 动作去重）。
        """
        self.remaining()
        if self.calls >= self.max_calls:
            raise StopRun('tool_budget')
        # 用 `[工具, 参数]` 的规范化 JSON 作唯一签名：同一工具同一参数不重复执行
        key = json.dumps([tool, arguments], ensure_ascii=False, sort_keys=True)
        if key in self.seen:
            raise StopRun('repeated_action')
        if tool == 'web_search' and self.web_calls >= self.max_web:
            raise StopRun('web_budget')
        self.seen.add(key)
        self.calls += 1
        self.web_calls += int(tool == 'web_search')  # 只有 web_search 计入联网次数

    def consume(self, text):
        """消耗一段文本的 token 计入总量；超过上限则抛 ``StopRun('context_budget')``。"""
        # 有本地 tokenizer 用精确 token 计数；否则按 UTF-8 字节数作为上界近似
        count = len(self.tokenizer.encode(text)) if self.tokenizer else len(text.encode('utf-8'))
        self.tokens += count
        if self.tokens > self.max_tokens:
            raise StopRun('context_budget')
        self.remaining()


def local_tokenizer(path):
    """从本地路径加载 tokenizer；路径为空或加载失败都返回 None（不强依赖联网）。"""
    if not path:
        return None
    try:
        from transformers import AutoTokenizer
        # local_files_only=True：禁止远程下载，保证离线可用、不触发网络
        return AutoTokenizer.from_pretrained(path, local_files_only=True)
    except (OSError, ValueError):
        # 本地文件缺失或格式不合法：降级为不用 tokenizer（走字节上界估算）
        return None