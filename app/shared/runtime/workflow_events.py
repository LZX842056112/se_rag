"""Store recent workflow runs and deliver independent monitor subscriptions."""
from __future__ import annotations

import asyncio
import json
import threading
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from dataclasses import dataclass

from app.shared.clients import redis_gateway
from app.shared.config import settings

ACTIVE = frozenset({"uploading", "pending", "processing", "retrying", "waiting"})
RECENT_LIMIT = 50
_lock = threading.Lock()
_runs: dict[str, dict[str, dict]] = {}
_removed: dict[tuple[str, str], float] = {}
_revision = 0
_listeners: dict[str, set] = defaultdict(set)

# The event and its latest state become visible to other workers together.
_WRITE = """
local action, id, ttl = ARGV[1], ARGV[2], tonumber(ARGV[5])
local old = redis.call('HGET', KEYS[1], id)
if action == 'run' and ARGV[4] ~= '*' then
    local revision = old and cjson.decode(old).revision or 0
    if old then
        if revision ~= tonumber(ARGV[4]) then return 0 end
    elseif redis.call('ZSCORE', KEYS[5], id) then
        return 0
    end
end
local clock = redis.call('TIME')
local timestamp = tonumber(clock[1]) * 1000000 + tonumber(clock[2])
local revision = math.max(timestamp, tonumber(redis.call('GET', KEYS[4]) or '0') + 1)
revision = string.format('%.0f', revision)
redis.call('SET', KEYS[4], revision)
local data = '{"revision":' .. revision .. ',"payload":' .. ARGV[3] .. '}'
if action == 'run' then
    redis.call('HSET', KEYS[1], id, data)
    redis.call('ZADD', KEYS[2], ARGV[7], id)
elseif action == 'remove' then
    redis.call('HDEL', KEYS[1], id)
    redis.call('ZREM', KEYS[2], id)
    redis.call('ZADD', KEYS[5], ARGV[8], id)
end
local expired = redis.call('ZRANGEBYSCORE', KEYS[2], '-inf', ARGV[6])
for _, key in ipairs(expired) do redis.call('HDEL', KEYS[1], key) end
redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', ARGV[6])
redis.call('ZREMRANGEBYSCORE', KEYS[5], '-inf', ARGV[6])
redis.call('XADD', KEYS[3], 'MAXLEN', 1024, '*', 'event', action, 'data', data)
for _, key in ipairs(KEYS) do redis.call('EXPIRE', key, ttl) end
redis.call('EXPIRE', KEYS[4], ttl * 2)
return revision
"""


def _keys(scope: str) -> tuple[str, ...]:
    return tuple(f"workflow:{{{scope}}}:{part}" for part in ("runs", "updated", "events", "revision", "removed"))


def _cutoff() -> float:
    return time.time() - settings.runtime.task_state_ttl_seconds


def _read_record(raw: str) -> dict:
    row = json.loads(raw)
    return {**row["payload"], "revision": row["revision"]}


def _prune_memory() -> None:
    cutoff = _cutoff()
    for scope, rows in list(_runs.items()):
        for run_id, row in list(rows.items()):
            if row["updated_at"] <= cutoff:
                del rows[run_id]
        if not rows:
            del _runs[scope]
    for key, removed_at in list(_removed.items()):
        if removed_at <= cutoff:
            del _removed[key]


def limit_runs(rows) -> list[dict]:
    ordered = sorted(rows, key=lambda row: (row["updated_at"], row["revision"]), reverse=True)
    active = [row for row in ordered if row["status"] in ACTIVE]
    recent = [row for row in ordered if row["status"] not in ACTIVE][:RECENT_LIMIT]
    return active + recent


def get_run(scope: str, run_id: str) -> dict:
    if redis_gateway.using_redis():
        raw = redis_gateway.get_redis_client().hget(_keys(scope)[0], run_id)
        row = _read_record(raw) if raw else {}
        return row if row and row["updated_at"] > _cutoff() else {}
    with _lock:
        _prune_memory()
        return _runs.get(scope, {}).get(run_id, {})


def list_runs(scope: str) -> list[dict]:
    if redis_gateway.using_redis():
        client = redis_gateway.get_redis_client()
        records, updated, *_ = _keys(scope)
        ids = client.zrangebyscore(updated, f"({_cutoff()}", "+inf")
        return limit_runs(_read_record(raw) for raw in client.hmget(records, ids) if raw) if ids else []
    with _lock:
        _prune_memory()
        return limit_runs(_runs.get(scope, {}).values())


def write(scope: str, event: str, data: dict | None = None, *, expected: int | None = None) -> int:
    """Publish a change, with an optional check for a newer writer of this run."""
    payload = dict(data or {})
    run_id = payload.get("id", "")
    if redis_gateway.using_redis():
        ttl = max(settings.runtime.task_state_ttl_seconds, 1)
        return int(redis_gateway.get_redis_client().eval(
            _WRITE, 5, *_keys(scope), event, run_id, json.dumps(payload),
            "*" if expected is None else expected, ttl, _cutoff(), payload.get("updated_at", 0), time.time(),
        ))

    global _revision
    with _lock:
        _prune_memory()
        rows = _runs.get(scope, {})
        if event == "run" and expected is not None:
            old = rows.get(run_id)
            if (old and old["revision"] != expected) or (not old and (scope, run_id) in _removed):
                return 0
        _revision = max(_revision + 1, time.time_ns() // 1000)
        payload["revision"] = _revision
        if event == "run":
            _runs.setdefault(scope, {})[run_id] = payload
        elif event == "remove":
            rows.pop(run_id, None)
            _removed[scope, run_id] = time.time()
        for receiver in _listeners.get(scope, ()):
            receiver.post(event, payload)
        return _revision


@dataclass(eq=False)
class _MemoryReceiver:
    loop: asyncio.AbstractEventLoop
    queue: asyncio.Queue

    def post(self, event: str, data: dict) -> None:
        def enqueue():
            if self.queue.full():
                while not self.queue.empty():
                    self.queue.get_nowait()
                self.queue.put_nowait(("resync", {}))
            self.queue.put_nowait((event, data))

        if not self.loop.is_closed():
            self.loop.call_soon_threadsafe(enqueue)

    async def get(self) -> tuple[str, dict]:
        return await self.queue.get()


class _RedisReceiver:
    def __init__(self, client, stream: str):
        self.client = client
        self.stream = stream
        self.cursor = "0-0"
        self.pending: asyncio.Queue = asyncio.Queue()

    async def anchor(self) -> None:
        last = await asyncio.to_thread(self.client.xrevrange, self.stream, count=1)
        self.cursor = last[0][0] if last else "0-0"

    async def get(self) -> tuple[str, dict]:
        while self.pending.empty():
            result = await asyncio.to_thread(self.client.xread, {self.stream: self.cursor}, count=64)
            if not result:
                await asyncio.sleep(0.25)
                continue
            entries = result[0][1]
            if self.cursor != "0-0":
                oldest = await asyncio.to_thread(self.client.xrange, self.stream, count=1)
                if oldest and tuple(map(int, oldest[0][0].split("-"))) > tuple(map(int, self.cursor.split("-"))):
                    await self.anchor()
                    return "resync", {}
            for self.cursor, fields in entries:
                self.pending.put_nowait((fields["event"], _read_record(fields["data"])))
        return self.pending.get_nowait()


@asynccontextmanager
async def subscribe(scope: str):
    """Attach before reading a snapshot, so changes during that read remain pending."""
    if redis_gateway.using_redis():
        receiver = _RedisReceiver(redis_gateway.get_redis_client(), _keys(scope)[2])
        await receiver.anchor()
        yield receiver
        return
    receiver = _MemoryReceiver(asyncio.get_running_loop(), asyncio.Queue(maxsize=256))
    with _lock:
        _listeners[scope].add(receiver)
    try:
        yield receiver
    finally:
        with _lock:
            _listeners[scope].remove(receiver)
            if not _listeners[scope]:
                del _listeners[scope]
