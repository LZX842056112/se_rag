"""Run with -m e2e and MULTI_WORKER_REDIS_URL set to an empty local Redis DB.

Use a disposable Redis instance. The tests start separate HTTP workers on
free loopback ports. Model, document, and scheduled work use local substitutes.
"""
from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
import socket
import threading
import time
import uuid
from contextlib import ExitStack, asynccontextmanager, contextmanager, suppress
from select import select
from socketserver import BaseRequestHandler, ThreadingTCPServer
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx
import pytest
from redis import Redis

pytestmark = pytest.mark.e2e

_TEST_LOCK = "se-rag:multi-worker-e2e"
_LEADER_KEY = "evolution:scheduler:leader"


def _run_worker(redis_url, prefix, snapshot_dir, run_scheduler, port_sender, stop):
    os.environ.update(PYTHON_DOTENV_DISABLED="1", LOG_FILE_ENABLE="0")

    import uvicorn
    from fastapi import FastAPI

    from app.api.errors import install_error_handlers
    from app.api.routers.query import router
    from app.rag.import_ import pipeline as imports
    from app.rag.query import pipeline as queries
    from app.shared.clients.redis_gateway import close_redis_client, get_redis_client
    from app.shared.config import settings
    from app.shared.utils import task_snapshot, task_state
    from app.shared.utils.sse_broker import SSEEvent, publish

    settings.redis.url = redis_url
    settings.runtime.state_backend = "redis"
    settings.runtime.query_turn_lock_ttl_seconds = 30
    settings.runtime.scheduler_leader_ttl_seconds = 30
    settings.runtime.sse_delta_flush_ms = 0
    settings.import_.max_concurrent_tasks = 1
    settings.import_.acquire_timeout_seconds = 1
    settings.import_.slot_lease_seconds = 60
    settings.api.session_ownership_enabled = False
    settings.evolution.write_enabled = False
    task_snapshot._SNAPSHOT_DIR = snapshot_dir

    gates: dict[str, threading.Event] = {}

    def run_query(state):
        session_id = state["session_id"]
        if state["original_query"] == "hold":
            gate = gates.setdefault(session_id, threading.Event())
            publish(session_id, SSEEvent.DELTA, {"delta": "partial"})
            if not gate.wait(timeout=90):
                raise TimeoutError("The test did not release the query")
        return {**state, "intent": "chitchat", "answer": "complete"}

    def run_import(state):
        if state["task_id"].endswith("-held"):
            get_redis_client().set(f"{state['task_id']}:started", str(os.getpid()))
            stop.wait(timeout=90)
        return {**state, "md_path": "test.md"}

    queries.query_app = SimpleNamespace(invoke=run_query)
    queries.record_query = lambda *args, **kwargs: None
    imports.import_app = SimpleNamespace(invoke=run_import)

    @asynccontextmanager
    async def lifespan(_app):
        async def stop_server():
            while not stop.is_set():
                await asyncio.sleep(0.05)
            server.should_exit = True

        stop_task = asyncio.create_task(stop_server())
        leader_task = None
        tick_client = None
        if run_scheduler:
            from app.evolution import governance, leader, scheduler

            # Record cycles even when the scheduler's lease connection is down.
            tick_client = Redis.from_url(os.environ["MULTI_WORKER_REDIS_URL"], socket_timeout=3)
            governance.run_publication_tasks = lambda: None
            scheduler.run_evolution_cycle_once = lambda: tick_client.rpush(
                f"{prefix}:ticks", str(os.getpid())
            )
            leader_task = asyncio.create_task(leader.scheduler_leader_loop(0.15))

        try:
            yield
        finally:
            tasks = [stop_task, leader_task] if leader_task else [stop_task]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            close_redis_client()
            if tick_client is not None:
                tick_client.close()

    app = FastAPI(lifespan=lifespan)
    install_error_handlers(app)
    app.include_router(router)

    @app.get("/test/pid")
    def pid():
        return os.getpid()

    @app.post("/test/finish-query/{session_id}")
    def finish_query(session_id: str):
        gates[session_id].set()

    @app.post("/test/import/{task_id}")
    def start_import(task_id: str):
        imports.submit_import_graph(task_id, "test.md", str(snapshot_dir))

    @app.get("/test/import/{task_id}")
    def import_status(task_id: str):
        return task_state.snapshot(task_id)

    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port_sender.send(listener.getsockname()[1])
        port_sender.close()
        server.run(sockets=[listener])


def _wait_for(check, timeout, description):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(0.05)
    pytest.fail(f"Timed out waiting for {description}")


@pytest.fixture
def redis_db():
    url = os.getenv("MULTI_WORKER_REDIS_URL")
    if not url:
        pytest.skip("Set MULTI_WORKER_REDIS_URL to an empty local Redis DB")
    if urlsplit(url).hostname not in {"127.0.0.1", "localhost", "::1"}:
        pytest.fail("Use a Redis instance on a loopback address")
    prefix = f"se-rag-e2e-{uuid.uuid4().hex}"

    with Redis.from_url(url, decode_responses=True, socket_timeout=3) as client:
        assert client.dbsize() == 0, "Use a disposable, empty Redis database"
        assert client.set(_TEST_LOCK, prefix, nx=True, ex=300)
        try:
            yield client, prefix
        finally:
            keys = list(client.scan_iter(match=f"*{prefix}*"))
            client.zrem("import:slots", f"{prefix}-held", f"{prefix}-busy", f"{prefix}-retry")
            client.delete(_TEST_LOCK, _LEADER_KEY, *keys)


@pytest.fixture
def redis_link(redis_db):
    """Interrupt one worker's connections to Redis through a loopback relay."""
    url = urlsplit(os.environ["MULTI_WORKER_REDIS_URL"])
    disconnected = threading.Event()
    rejected = threading.Event()

    class RelayHandler(BaseRequestHandler):
        def handle(self):
            if disconnected.is_set():
                rejected.set()
                return
            with (
                suppress(OSError),
                socket.create_connection((url.hostname, url.port or 6379), timeout=3) as upstream,
            ):
                sockets = (self.request, upstream)
                while not disconnected.is_set():
                    readable, _, _ = select(sockets, (), (), 0.05)
                    for source in readable:
                        data = source.recv(65536)
                        if not data:
                            return
                        target = upstream if source is self.request else self.request
                        target.sendall(data)

    class RelayServer(ThreadingTCPServer):
        address_family = socket.AF_INET6 if url.hostname == "::1" else socket.AF_INET
        daemon_threads = True

    @contextmanager
    def disconnect():
        rejected.clear()
        disconnected.set()
        try:
            yield rejected
        finally:
            disconnected.clear()

    with RelayServer((url.hostname, 0), RelayHandler) as relay:
        thread = threading.Thread(target=relay.serve_forever)
        thread.start()
        credentials, separator, _ = url.netloc.rpartition("@")
        host = "[::1]" if url.hostname == "::1" else url.hostname
        relay_url = url._replace(netloc=f"{credentials}{separator}{host}:{relay.server_address[1]}").geturl()
        try:
            yield relay_url, disconnect
        finally:
            disconnected.set()
            relay.shutdown()
            thread.join()


@pytest.fixture
def start_worker(redis_db, tmp_path):
    _, prefix = redis_db
    url = os.environ["MULTI_WORKER_REDIS_URL"]
    ctx = multiprocessing.get_context("spawn")

    @contextmanager
    def worker(scheduler, redis_url):
        receiver, sender = ctx.Pipe(duplex=False)
        stop = ctx.Event()
        process = ctx.Process(
            target=_run_worker,
            args=(redis_url, prefix, tmp_path, scheduler, sender, stop),
        )
        process.start()
        sender.close()
        try:
            assert receiver.poll(45), "The worker did not open an HTTP port"
            base_url = f"http://127.0.0.1:{receiver.recv()}"
            with httpx.Client(base_url=base_url, trust_env=False, timeout=10) as http:
                def is_ready():
                    assert process.is_alive(), f"Worker exit code: {process.exitcode}"
                    with suppress(httpx.ConnectError):
                        return http.get("/test/pid").raise_for_status().json() == process.pid

                _wait_for(is_ready, 10, "worker HTTP startup")
                yield http, process
        finally:
            receiver.close()
            if process.is_alive():
                stop.set()
                process.join(5)
            if process.is_alive():
                process.kill()
                process.join(5)
            assert not process.is_alive(), f"Worker {process.pid} did not stop"
            process.close()

    with ExitStack() as stack:
        def start(*, scheduler=False, redis_url=url):
            return stack.enter_context(worker(scheduler, redis_url))

        yield start


def _submit(worker, session, query):
    return worker.post("/api/query", json={"session_id": session, "query": query, "is_stream": True})


def _events(response):
    event = ""
    for line in response.raise_for_status().iter_lines():
        if line.startswith("event: "):
            event = line[7:]
        elif line.startswith("data: "):
            yield event, json.loads(line[6:])


def _kill(process):
    process.kill()
    process.join(5)
    assert not process.is_alive()
    assert process.exitcode != 0


def test_query_exclusion_and_sse_replay_with_malformed_cursor(redis_db, start_worker):
    client, session = redis_db
    producer, producer_process = start_worker()
    subscriber, subscriber_process = start_worker()
    assert producer_process.pid != subscriber_process.pid

    turn_id = _submit(producer, session, "hold").raise_for_status().json()["turn_id"]
    with subscriber.stream("GET", f"/api/stream/{session}") as response:
        for event, data in _events(response):
            if event == "delta":
                assert data == {"delta": "partial"}
                break
        else:
            pytest.fail("The first connection did not receive the partial answer")

        rejected = _submit(subscriber, session, "conflicting query")
        assert rejected.status_code == 409
        assert rejected.json()["code"] == "query_in_progress"
        state = subscriber.get(f"/api/query/status/{session}").raise_for_status().json()
        assert (state["status"], state["query"], state["turn_id"]) == ("processing", "hold", turn_id)

    client.xadd(f"sse:stream:{session}", {"event": "progress", "data": "{invalid"})
    producer.post(f"/test/finish-query/{session}").raise_for_status()
    with producer.stream(
        "GET", f"/api/stream/{session}", headers={"Last-Event-ID": "invalid-stream-id"}
    ) as response:
        events = list(_events(response))

    assert [event for event, _ in events] == [
        "ready", "progress", "delta", "progress", "progress", "final", "close",
    ]
    assert events[1][1]["status"] == "processing"
    assert events[2] == ("delta", {"delta": "partial"})
    assert events[3] == ("progress", {})
    assert events[4][1]["status"] == "completed"
    assert events[-2][1]["answer"] == "complete"
    assert events[-2][1]["turn_id"] == turn_id


def test_sse_waits_for_redis_on_reconnect(redis_db, redis_link, start_worker):
    _, session = redis_db
    relay_url, disconnect = redis_link
    producer, _ = start_worker()
    subscriber, _ = start_worker(redis_url=relay_url)
    stream_url = f"/api/stream/{session}"
    turn_id = _submit(producer, session, "hold").raise_for_status().json()["turn_id"]
    with producer.stream("GET", stream_url) as response:
        events = _events(response)
        assert next(events) == ("ready", {})
        assert next(events)[0] == "progress"
        assert next(events) == ("delta", {"delta": "partial"})
    producer.post(f"/test/finish-query/{session}").raise_for_status()

    with ExitStack() as connection:
        with disconnect() as rejected:
            response = connection.enter_context(subscriber.stream("GET", stream_url))
            assert rejected.wait(5), "The reconnect did not reach the interrupted Redis link"
        events = list(_events(response))

    assert [event for event, _ in events] == ["ready", "progress", "delta", "progress", "final", "close"]
    assert events[2] == ("delta", {"delta": "partial"})
    assert events[-2][1]["turn_id"] == turn_id


def test_query_admission_and_open_sse_recover_after_redis_outage(redis_db, redis_link, start_worker):
    client, session = redis_db
    relay_url, disconnect = redis_link
    producer, _ = start_worker()
    subscriber, _ = start_worker(redis_url=relay_url)
    turn_id = _submit(producer, session, "hold").raise_for_status().json()["turn_id"]

    with subscriber.stream("GET", f"/api/stream/{session}") as response:
        events = _events(response)
        assert next(events) == ("ready", {})
        assert next(events)[0] == "progress"
        assert next(events) == ("delta", {"delta": "partial"})

        with disconnect() as rejected:
            assert rejected.wait(5), "The open stream did not try to read through the interrupted Redis link"
            failed = _submit(subscriber, f"{session}-retry", "retry")
            assert failed.status_code == 500
            assert failed.json()["code"] == "internal_error"
            assert not client.exists(f"query:active:{session}-retry")
            producer.post(f"/test/finish-query/{session}").raise_for_status()

        remaining = list(events)

    assert [event for event, _ in remaining] == ["progress", "final", "close"]
    assert remaining[1][1]["turn_id"] == turn_id
    _submit(subscriber, f"{session}-retry", "retry").raise_for_status()


def test_query_and_import_can_restart_after_worker_crash(redis_db, start_worker):
    client, session = redis_db
    owner, owner_process = start_worker()
    standby, _ = start_worker()
    held = f"{session}-held"
    busy = f"{session}-busy"
    retry = f"{session}-retry"
    import_url = "/test/import"

    turn_id = _submit(owner, session, "hold").raise_for_status().json()["turn_id"]
    owner.post(f"{import_url}/{held}").raise_for_status()
    started_pid = _wait_for(lambda: client.get(f"{held}:started"), 10, "import graph startup")
    assert started_pid == str(owner_process.pid)
    _kill(owner_process)

    rejected = _submit(standby, session, "retry")
    assert rejected.status_code == 409
    standby.post(f"{import_url}/{busy}").raise_for_status()
    busy_state = _wait_for(
        lambda: standby.get(f"{import_url}/{busy}").raise_for_status().json().get("error_code"),
        5,
        "the import slot wait to expire",
    )
    assert busy_state == "import_busy"

    def try_query():
        response = _submit(standby, session, "retry")
        if response.status_code == 409:
            return None
        return response.raise_for_status().json()

    replacement = _wait_for(try_query, 35, "the query lock to expire")
    assert replacement["turn_id"] != turn_id

    assert client.zscore("import:slots", held) is not None
    _wait_for(lambda: client.zscore("import:slots", held) <= time.time(), 65, "the import lease to expire")
    standby.post(f"{import_url}/{retry}").raise_for_status()
    _wait_for(
        lambda: standby.get(f"{import_url}/{retry}").raise_for_status().json().get("status") == "completed",
        5,
        "the replacement import to complete",
    )
    assert client.zscore("import:slots", held) is None


def test_scheduler_renews_and_takes_over_after_worker_crash(redis_db, start_worker):
    client, prefix = redis_db
    _, first = start_worker(scheduler=True)
    tick_key = f"{prefix}:ticks"
    _wait_for(lambda: client.llen(tick_key) > 0, 10, "the first scheduled cycle")
    _, second = start_worker(scheduler=True)
    token = client.get(_LEADER_KEY)
    assert token

    _wait_for(lambda: 0 < client.pttl(_LEADER_KEY) < 25_000, 10, "the leader lease to count down")
    _wait_for(lambda: client.pttl(_LEADER_KEY) > 28_000, 15, "the leader to renew its lease")
    assert client.get(_LEADER_KEY) == token
    assert set(client.lrange(tick_key, 0, -1)) == {str(first.pid)}

    _kill(first)
    finished_cycles = client.llen(tick_key)
    _wait_for(lambda: client.llen(tick_key) > finished_cycles, 45, "the standby to take over")
    assert set(client.lrange(tick_key, finished_cycles, -1)) == {str(second.pid)}
    assert client.get(_LEADER_KEY) not in {None, token}


def test_scheduler_stops_after_redis_renewal_error(redis_db, redis_link, start_worker):
    client, prefix = redis_db
    relay_url, disconnect = redis_link
    tick_key = f"{prefix}:ticks"
    with disconnect() as rejected:
        _, process = start_worker(scheduler=True, redis_url=relay_url)
        assert rejected.wait(5), "The scheduler did not try to acquire the lease"
        assert not client.exists(_LEADER_KEY)
        assert not client.exists(tick_key)

    _wait_for(lambda: client.llen(tick_key) >= 2, 15, "scheduled cycles after Redis recovers")
    token = client.get(_LEADER_KEY)
    assert token

    def cycles_stopped():
        before = client.llen(tick_key)
        time.sleep(0.6)
        return client.llen(tick_key) == before

    with disconnect() as rejected:
        assert rejected.wait(15), "The scheduler did not try to renew the lease"
        _wait_for(cycles_stopped, 5, "scheduled cycles to stop after the renewal fails")
        assert client.get(_LEADER_KEY) == token
        finished_cycles = client.llen(tick_key)
        assert client.set(_LEADER_KEY, "another-owner", xx=True, ex=30)

    assert cycles_stopped()
    assert client.llen(tick_key) == finished_cycles
    assert client.get(_LEADER_KEY) == "another-owner"
    client.delete(_LEADER_KEY)
    _wait_for(lambda: client.llen(tick_key) > finished_cycles, 15, "the worker to reacquire the lease")
    assert set(client.lrange(tick_key, 0, -1)) == {str(process.pid)}

    assert client.set(_LEADER_KEY, "replacement-owner", xx=True, ex=30)
    _wait_for(cycles_stopped, 15, "scheduled cycles to stop after the lease changes owner")
    assert client.get(_LEADER_KEY) == "replacement-owner"
