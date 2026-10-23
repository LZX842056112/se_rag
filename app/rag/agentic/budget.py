"""逐请求的限制与稳定的会话放量。不进行远程 tokenizer 下载。"""
from __future__ import annotations

import hashlib
import json
import math
import time


class StopRun(RuntimeError):
    pass


def selected_mode(session_id, *, mode='legacy', percent=0):
    bucket = int(hashlib.sha256(str(session_id).encode()).hexdigest()[:8], 16) % 100
    return 'agentic' if mode == 'agentic' and bucket < percent else 'legacy'


class Budget:
    def __init__(self, *, max_calls=4, max_web=1, max_tokens=8000, seconds=60,
                 clock=time.monotonic, tokenizer=None):
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError('deadline must be finite and positive')
        self.clock, self.started = clock, clock()
        self.deadline = self.started + seconds
        self.max_calls, self.max_web, self.max_tokens = max_calls, max_web, max_tokens
        self.calls = self.web_calls = self.tokens = 0
        self.seen = set()
        self.tokenizer = tokenizer
        self.counting = 'local_tokenizer' if tokenizer else 'utf8_bytes_upper_bound'

    def remaining(self):
        remaining = self.deadline - self.clock()
        if remaining <= 0:
            raise StopRun('deadline')
        return remaining

    def take(self, tool, arguments):
        self.remaining()
        if self.calls >= self.max_calls:
            raise StopRun('tool_budget')
        key = json.dumps([tool, arguments], ensure_ascii=False, sort_keys=True)
        if key in self.seen:
            raise StopRun('repeated_action')
        if tool == 'web_search' and self.web_calls >= self.max_web:
            raise StopRun('web_budget')
        self.seen.add(key)
        self.calls += 1
        self.web_calls += int(tool == 'web_search')

    def consume(self, text):
        count = len(self.tokenizer.encode(text)) if self.tokenizer else len(text.encode('utf-8'))
        self.tokens += count
        if self.tokens > self.max_tokens:
            raise StopRun('context_budget')
        self.remaining()


def local_tokenizer(path):
    if not path:
        return None
    try:
        from transformers import AutoTokenizer
        return AutoTokenizer.from_pretrained(path, local_files_only=True)
    except (OSError, ValueError):
        return None