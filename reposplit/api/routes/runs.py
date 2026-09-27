"""Run execution, status, approval, and SSE event streaming endpoints."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from reposplit.api.config import settings
from reposplit.api.models import RunManager, StartRun
from reposplit.api.routes.ingestion import _clone_git_repo, is_git_url
from reposplit.core.schemas import DependencyGraph, DomainTopology, Keys, Phase

router = APIRouter(prefix="/api", tags=["runs"])


def _manager(request: Request) -> RunManager:
    return request.app.state.manager


@router.post("/runs")
async def start_run(req: StartRun, request: Request) -> dict[str, Any]:
    manager = _manager(request)
    repo_path = req.repo_path.strip()
    if is_git_url(repo_path):
        clone_id = uuid.uuid4().hex[:8]
        target_dir = settings.storage_dir / "clones" / clone_id
        cloned_dir = _clone_git_repo(repo_path, target_dir)
        req.repo_path = str(cloned_dir).replace("\\", "/")
    elif not Path(req.repo_path).is_dir():
        raise HTTPException(400, f"repo_path not found: {req.repo_path}")
    handle = manager.start(req)
    return {"run_id": handle.supervisor.run_id, "phase": handle.supervisor.phase}


@router.get("/runs")
async def list_runs(request: Request) -> list[dict[str, Any]]:
    manager = _manager(request)
    return [
        {"run_id": rid, "phase": h.supervisor.phase, "repo": h.supervisor.config.repo_path, "done": h.task.done() if h.task else False}
        for rid, h in manager.runs.items()
    ]


@router.get("/runs/{run_id}")
async def get_run(run_id: str, request: Request) -> dict[str, Any]:
    manager = _manager(request)
    h = manager.get(run_id)
    return {
        "run_id": run_id,
        "phase": h.supervisor.phase,
        "summary": h.supervisor.summary.model_dump(mode="json"),
        "transitions": [t.__dict__ for t in h.supervisor.fsm.history],
        "keys": h.supervisor.bb.keys(),
        "awaiting_approval": h.supervisor.phase == Phase.STRANGLER and not h.supervisor.ctx.approval.is_set(),
    }


@router.post("/runs/{run_id}/approve")
async def approve(run_id: str, request: Request) -> dict[str, str]:
    manager = _manager(request)
    manager.get(run_id).supervisor.approve()
    return {"status": "approved"}


@router.get("/runs/{run_id}/state/{key}")
async def state(run_id: str, key: str, request: Request) -> Any:
    manager = _manager(request)
    bb = manager.get(run_id).supervisor.bb
    if not bb.has(key):
        raise HTTPException(404, f"key {key} not on blackboard yet")
    value = bb.get(key)
    return value.model_dump(mode="json", by_alias=True) if isinstance(value, BaseModel) else value


@router.get("/runs/{run_id}/graph")
async def graph(run_id: str, request: Request) -> dict[str, Any]:
    manager = _manager(request)
    bb = manager.get(run_id).supervisor.bb
    if not bb.has(Keys.DEPENDENCY_GRAPH):
        raise HTTPException(404, "graph not available yet")
    g = bb.get(Keys.DEPENDENCY_GRAPH, DependencyGraph)
    topo = bb.get(Keys.TOPOLOGY, DomainTopology) if bb.has(Keys.TOPOLOGY) else None
    cluster_of = {}
    if topo:
        for name, c in topo.clusters.items():
            for s in c.symbols:
                cluster_of[s] = name
    nodes = [
        {"id": n.id, "name": n.name, "kind": n.kind, "module": n.module, "cluster": cluster_of.get(n.id, "unassigned"), "is_model": n.is_model, "route": n.route.path if n.route else None}
        for n in g.nodes
        if n.kind in ("class", "function")
    ]
    ids = {n["id"] for n in nodes}
    edges = [e.model_dump() for e in g.edges if e.source in ids and e.target in ids]
    severed = {(s.source, s.target) for s in topo.severed_edges} if topo else set()
    for e in edges:
        e["severed"] = (e["source"], e["target"]) in severed
    return {"nodes": nodes, "edges": edges, "clusters": sorted(set(cluster_of.values()))}


@router.get("/runs/{run_id}/events")
async def events(run_id: str, request: Request) -> StreamingResponse:
    manager = _manager(request)
    h = manager.get(run_id)
    bb = h.supervisor.bb

    async def stream():
        queue = bb.subscribe()
        try:
            for ev in bb.events():
                yield f"id: {ev.seq}\nevent: telemetry\ndata: {ev.model_dump_json()}\n\n"
            while True:
                if h.task and h.task.done() and queue.empty():
                    yield "event: end\ndata: {}\n\n"
                    return
                try:
                    ev = await asyncio.wait_for(queue.get(), timeout=1.0)
                except TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield f"id: {ev.seq}\nevent: telemetry\ndata: {ev.model_dump_json()}\n\n"
        finally:
            bb.unsubscribe(queue)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/agents")
async def agents() -> list[dict[str, str]]:
    from reposplit.core.registry import describe_agents

    return describe_agents()
