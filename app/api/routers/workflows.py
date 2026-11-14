"""Read-only feeds for the live workflow page."""
from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from typing import Annotated, Literal

import httpx
from anyio import CancelScope
from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.responses import StreamingResponse

from app.api.errors import ApiError
from app.api.routers.evolution import require_admin_token
from app.api.routers.import_ import require_import_admin_token
from app.api.session_access import authorize_session, session_owner_hash
from app.api.workflow_graphs import legacy_nodes, workflow_graph
from app.evolution.flags import writes_enabled
from app.evolution.workflow_candidates import list_candidate_runs
from app.rag.import_.task_records import is_valid_task_id
from app.shared.clients.mongo import get_collection
from app.shared.config import settings
from app.shared.runtime import workflow_events
from app.shared.runtime.workflow_monitor import CANDIDATE, CYCLE, IMPORT, QUERY, query_scope
from app.shared.utils import task_snapshot, task_state

query_router = APIRouter(prefix="/api/workflows", tags=["workflows"])
import_router = APIRouter(prefix="/api/workflows", tags=["workflows"])
_HEADERS = {"Cache-Control": "no-cache, no-store", "X-Accel-Buffering": "no"}
_HEARTBEAT = 15.0


def _legacy_run(view: str, run_id: str, data: dict, timestamp: float, *, interrupted: bool = False) -> dict:
    status = data.get("status") or "unknown"
    if interrupted and status in workflow_events.ACTIVE:
        status = "interrupted"
    nodes = legacy_nodes(view, data.get("done_list") or [], data.get("running_list") or [])
    if status in {"completed", "failed", "interrupted"}:
        for node in nodes.values():
            if node["status"] == "running":
                node["status"] = "unknown"
    if status == "completed":
        nodes["__end__"] = {"status": "completed", "visits": 1}
    return {"id": run_id, "label": "文件导入" if view == IMPORT else "问答", "status": status,
            "nodes": nodes, "attempt": data.get("workflow_attempt", 1), "revision": 0, "legacy": True,
            "created_at": timestamp, "updated_at": timestamp, "error_code": data.get("error_code")}


def _imports() -> list[dict]:
    rows = {row["id"]: row for row in workflow_events.list_runs(IMPORT)}
    cutoff = time.time() - settings.runtime.task_state_ttl_seconds
    for run_id, timestamp in task_snapshot.recent_snapshot_ids(cutoff):
        if run_id in rows or not is_valid_task_id(run_id):
            continue
        current = task_state.snapshot(run_id)
        saved = current or task_snapshot.load_snapshot(run_id)
        if saved:
            rows[run_id] = _legacy_run(IMPORT, run_id, saved, timestamp, interrupted=not current)
    return workflow_events.limit_runs(rows.values())


def _queries(session_id: str, scope: str, request: Request) -> list[dict]:
    rows = {row["id"]: row for row in workflow_events.list_runs(scope)}
    # An unbound session can wait on its private feed. Legacy data requires a bound owner.
    owner_hash = session_owner_hash(request)
    owners = get_collection(settings.mongo.query_sessions_collection)
    if not owners.find_one({"_id": session_id, "owner_hash": owner_hash}, {"_id": 1}):
        return list(rows.values())
    current = task_state.snapshot(session_id, include_updated_at=True)
    run_id = current.get("turn_id")
    if run_id and run_id not in rows:
        rows[run_id] = _legacy_run(QUERY, run_id, current, current["updated_at"])
    cutoff = time.time() - settings.runtime.task_state_ttl_seconds
    history = get_collection(settings.mongo.query_runs_collection).find(
        {"session_id": session_id, "ts": {"$gte": cutoff}}, {"turn_id": 1, "ts": 1, "status": 1},
    ).sort("ts", -1).limit(workflow_events.RECENT_LIMIT)
    for saved in history:
        key = saved.get("turn_id")
        if key and key not in rows:
            rows[key] = _legacy_run(QUERY, key, saved, saved["ts"])
    return workflow_events.limit_runs(rows.values())


def _configuration(view: str) -> dict:
    if view == CYCLE:
        return {"enabled": writes_enabled() and settings.evolution.scheduler_enabled}
    if view == CANDIDATE:
        return {"enabled": writes_enabled()}
    return {"enabled": True}


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _events(view: str, scope: str, rows: Callable[[], list[dict]]):
    async with workflow_events.subscribe(scope) as receiver:
        graph = await asyncio.to_thread(workflow_graph, view)
        initial = await asyncio.to_thread(rows)
        if view == CANDIDATE:
            initial = workflow_events.limit_runs(initial)
        known = {row["id"]: row for row in initial}
        removed = {}
        revision = 0
        configuration = _configuration(view)
        yield _sse("snapshot", {"view": view, "graph": graph, "runs": initial, **configuration})
        while True:
            try:
                event, data = await asyncio.wait_for(receiver.get(), _HEARTBEAT)
            except TimeoutError:
                event, data = "heartbeat", {}

            if event in {"run", "remove"}:
                key = data["id"]
                seen = max(known.get(key, {}).get("revision", 0), removed.get(key, (0, 0))[0])
                if data["revision"] <= seen:
                    continue
                if event == "run":
                    known[key] = data
                    removed.pop(key, None)
                else:
                    known.pop(key, None)
                    removed[key] = (data["revision"], time.time())
                yield _sse(event, data)
                continue

            # Refresh durable candidate data after writes. The heartbeat also refreshes leases.
            current = await asyncio.to_thread(rows)
            current_config = _configuration(view)
            if event == "resync" or current_config != configuration or (
                view != CANDIDATE and any(row["id"] not in known or known[row["id"]] != row for row in current)
            ):
                configuration = current_config
                if view == CANDIDATE:
                    current = workflow_events.limit_runs(current)
                known = {row["id"]: row for row in current}
                removed.clear()
                yield _sse("snapshot", {"view": view, "graph": graph, "runs": current, **configuration})
            elif view == CANDIDATE:
                latest = {row["id"]: row for row in current}
                revision = max(revision + 1, time.time_ns() // 1000, data.get("revision", 0))
                for key in known.keys() - latest.keys():
                    yield _sse("remove", {"id": key, "revision": revision})
                target = {row["id"]: row for row in workflow_events.limit_runs(current)}
                target.update({key: latest[key] for key in known.keys() & latest.keys()})
                for key, row in target.items():
                    if known.get(key) != row:
                        yield _sse("run", {**row, "revision": revision})
                known = target
            else:
                cutoff = time.time() - settings.runtime.task_state_ttl_seconds
                live_ids = {row["id"] for row in current}
                expired = [key for key, row in known.items() if key not in live_ids and row["updated_at"] <= cutoff]
                for key in expired:
                    row = known.pop(key)
                    removed[key] = (row["revision"], time.time())
                    yield _sse("remove", {"id": key, "revision": row["revision"], "expired": True})
                removed = {key: info for key, info in removed.items() if info[1] > cutoff}
            if event == "heartbeat":
                yield ": heartbeat\n\n"


def _response(view: str, scope: str, rows: Callable[[], list[dict]]) -> StreamingResponse:
    return StreamingResponse(_events(view, scope, rows), media_type="text/event-stream", headers=_HEADERS)


@import_router.get("/import/events", dependencies=[Depends(require_import_admin_token)])
def import_events():
    return _response(IMPORT, IMPORT, _imports)


@query_router.get("/import/events")
async def proxy_import_events(
    x_internal_token: Annotated[str, Header()] = "",
):
    require_import_admin_token(x_internal_token)
    origin = settings.api.workflow_import_origin or f"http://127.0.0.1:{settings.api.import_port}"
    client = httpx.AsyncClient(timeout=httpx.Timeout(5.0, read=35.0), trust_env=False)
    upstream = None

    async def close_upstream():
        with CancelScope(shield=True):
            try:
                if upstream is not None:
                    await upstream.aclose()
            finally:
                await client.aclose()

    try:
        request = client.build_request("GET", origin.rstrip("/") + "/api/workflows/import/events",
                                       headers={"X-Internal-Token": x_internal_token})
        upstream = await client.send(request, stream=True)
        if upstream.status_code != 200 or "text/event-stream" not in upstream.headers.get("content-type", ""):
            raise ApiError("workflow_import_unavailable", "暂时无法读取导入流程", 503)
    except BaseException as exc:
        await close_upstream()
        if isinstance(exc, httpx.HTTPError):
            raise ApiError("workflow_import_unavailable", "暂时无法连接导入服务", 503) from exc
        raise

    async def proxy():
        try:
            async for chunk in upstream.aiter_bytes():
                yield chunk
        finally:
            await close_upstream()

    return StreamingResponse(proxy(), media_type="text/event-stream", headers=_HEADERS)


@query_router.get("/query/events")
def query_events(request: Request, session_id: Annotated[str, Query(min_length=1, max_length=128)]):
    if not settings.api.session_ownership_enabled:
        raise ApiError("workflow_ownership_required", "请启用会话归属校验后查看问答流程", 503)
    authorize_session(request, session_id)
    scope = query_scope(session_id, session_owner_hash(request))
    return _response(QUERY, scope, lambda: _queries(session_id, scope, request))


@query_router.get("/{view}/events", dependencies=[Depends(require_admin_token)])
def evolution_events(view: Literal["evolution-cycle", "evolution-candidate"]):
    rows = (lambda: list_candidate_runs(limit=False)) if view == CANDIDATE else lambda: workflow_events.list_runs(CYCLE)
    return _response(view, view, rows)


@query_router.post("/internal/candidate-refresh", status_code=204, dependencies=[Depends(require_import_admin_token)])
def refresh_candidates():
    workflow_events.write(CANDIDATE, "refresh")
    return Response(status_code=204)
