"""Observe workflow execution without changing the workflow result."""
from __future__ import annotations

import hashlib
import json
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar

from app.shared.runtime import workflow_events
from app.shared.runtime.logger import logger

IMPORT = "import"
QUERY = "query"
CYCLE = "evolution-cycle"
CANDIDATE = "evolution-candidate"


def query_scope(session_id: str, owner_hash: str) -> str:
    scope = hashlib.sha256(json.dumps([session_id, owner_hash]).encode()).hexdigest()
    return QUERY + ":" + scope


_forward_candidate_updates: ContextVar[bool] = ContextVar("workflow_forward_candidates", default=False)


@contextmanager
def forward_candidate_updates():
    """Send import-side changes to the query service when processes do not share Redis."""
    token = _forward_candidate_updates.set(True)
    try:
        yield
    finally:
        _forward_candidate_updates.reset(token)


def candidate_changed() -> None:
    """Wake candidate readers after a committed write. MongoDB owns the state."""
    try:
        from app.shared.clients import redis_gateway
        if not _forward_candidate_updates.get() or redis_gateway.using_redis():
            workflow_events.write(CANDIDATE, "refresh")
            return

        from app.shared.config import settings
        if settings.import_.admin_token:
            import httpx

            origin = settings.api.workflow_query_origin or f"http://127.0.0.1:{settings.api.query_port}"
            with httpx.Client(timeout=1.0, trust_env=False) as client:
                client.post(
                    origin.rstrip("/") + "/api/workflows/internal/candidate-refresh",
                    headers={"X-Internal-Token": settings.import_.admin_token},
                ).raise_for_status()
    except Exception as exc:
        logger.warning(f"发送候选流程更新失败：{type(exc).__name__}")


_active: ContextVar[Run | None] = ContextVar("workflow_run", default=None)


class Run:
    def __init__(self, scope: str, run_id: str):
        self.scope = scope
        self.id = run_id
        self.lock = threading.RLock()
        try:
            self.data = workflow_events.get_run(scope, run_id)
        except Exception as exc:
            logger.warning(f"读取流程状态失败：{type(exc).__name__}")
            self.data = {}

    @contextmanager
    def activate(self):
        token = _active.set(self)
        try:
            yield
        finally:
            _active.reset(token)

    def _write(self, **changes):
        row = {**self.data, **changes, "id": self.id, "updated_at": time.time()}
        try:
            revision = workflow_events.write(self.scope, "run", row, expected=self.data.get("revision", 0))
            if not revision:
                return
            row["revision"] = revision
        except Exception as exc:
            logger.warning(f"发送流程状态失败：{type(exc).__name__}")
        self.data = row

    def begin(self, status: str, label: str | None = None, *, keep: tuple[str, ...] = (),
              previous_attempt: int = 0) -> None:
        with self.lock:
            now = time.time()
            self._write(
                status=status, label=label or self.data.get("label") or self.id,
                created_at=self.data.get("created_at", now), attempt=self.data.get("attempt", previous_attempt) + 1,
                nodes={key: value for key, value in self.data.get("nodes", {}).items() if key in keep},
                error_code=None, detail=None,
            )

    def status(self, status: str, *, error_code: str | None = None) -> None:
        with self.lock:
            if self.data.get("status") not in workflow_events.ACTIVE:
                return
            nodes = self.data["nodes"]
            if status == "retrying":
                nodes = {key: ({**node, "status": "retrying"} if node["status"] == "failed" else node)
                         for key, node in nodes.items()}
            elif status not in workflow_events.ACTIVE:
                nodes = {key: ({**node, "status": "interrupted"} if node["status"] == "running" else node)
                         for key, node in nodes.items()}
                if status == "completed":
                    nodes["__end__"] = {"status": "completed", "visits": 1}
            self._write(status=status, nodes=nodes, error_code=error_code)

    def node(self, node_id: str, status: str, *, detail: str | None = None) -> None:
        with self.lock:
            if self.data.get("status") not in workflow_events.ACTIVE:
                return
            nodes = self.data["nodes"]
            visits = nodes.get(node_id, {}).get("visits", 0) + int(status == "running")
            change = {"status": status, "visits": visits, "detail": detail}
            if nodes.get(node_id) != change:
                self._write(nodes={**nodes, node_id: change})

    def remove(self) -> None:
        with self.lock:
            try:
                workflow_events.write(self.scope, "remove", {"id": self.id})
            except Exception as exc:
                logger.warning(f"删除流程状态失败：{type(exc).__name__}")
            self.data = {}


def node_status(node_id: str, status: str, *, detail: str | None = None) -> None:
    run = _active.get()
    if run is not None:
        run.node(node_id, status, detail=detail)


@contextmanager
def observe(node_id: str):
    node_status(node_id, "running")
    try:
        yield
    except BaseException:
        node_status(node_id, "failed")
        raise
    else:
        run = _active.get()
        if run is not None:
            with run.lock:
                if run.data.get("nodes", {}).get(node_id, {}).get("status") == "running":
                    run.node(node_id, "completed")
