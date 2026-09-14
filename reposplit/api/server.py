"""Telemetry API + dashboard host.

    POST /api/runs                 start a run   {repo_path, provider?, mode?, live?, require_approval?}
    GET  /api/runs                 list runs
    GET  /api/runs/{id}            run summary + current phase
    GET  /api/runs/{id}/events     Server-Sent Events: replay + live telemetry
    GET  /api/runs/{id}/graph      nodes/edges/clusters for the D3 untangling view
    GET  /api/runs/{id}/state/{key}   any blackboard key (e.g. architect.topology)
    POST /api/runs/{id}/approve    human oversight gate
    GET  /                         dashboard (static)

The React/D3 dashboard consumes exactly these endpoints; the bundled index.html is a minimal
reference client.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from reposplit.core.schemas import DependencyGraph, DomainTopology, Keys, Phase, RunConfig
from reposplit.core.supervisor import Supervisor

STATIC = Path(__file__).parent / "static"


class StartRun(BaseModel):
    repo_path: str
    output_dir: str | None = None
    provider: str = "auto"
    mode: str = "full"
    target_services: list[str] = []
    live: bool = False
    require_approval: bool = True
    canary_weight: int = 10


@dataclass
class RunHandle:
    supervisor: Supervisor
    task: asyncio.Task | None = None
    created: str = field(default_factory=lambda: uuid.uuid4().hex)


class RunManager:
    def __init__(self) -> None:
        self.runs: dict[str, RunHandle] = {}

    def start(self, req: StartRun) -> RunHandle:
        run_id = uuid.uuid4().hex[:12]
        config = RunConfig(
            repo_path=req.repo_path,
            output_dir=req.output_dir or f"out/{run_id}",
            provider=req.provider,  # type: ignore[arg-type]
            mode=req.mode,  # type: ignore[arg-type]
            target_services=req.target_services,
            live=req.live,
            require_approval=req.require_approval,
            canary_weight=req.canary_weight,
        )
        handle = RunHandle(supervisor=Supervisor(config, run_id=run_id))
        handle.task = asyncio.create_task(handle.supervisor.run())
        self.runs[run_id] = handle
        return handle

    def get(self, run_id: str) -> RunHandle:
        if run_id not in self.runs:
            raise HTTPException(404, f"unknown run {run_id}")
        return self.runs[run_id]


def create_app() -> FastAPI:
    app = FastAPI(title="RepoSplit 2.0 telemetry", version="2.0")
    manager = RunManager()
    app.state.manager = manager

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.post("/api/runs")
    async def start_run(req: StartRun) -> dict[str, Any]:
        if not Path(req.repo_path).is_dir():
            raise HTTPException(400, f"repo_path not found: {req.repo_path}")
        handle = manager.start(req)
        return {"run_id": handle.supervisor.run_id, "phase": handle.supervisor.phase}

    @app.get("/api/runs")
    async def list_runs() -> list[dict[str, Any]]:
        return [
            {"run_id": rid, "phase": h.supervisor.phase, "repo": h.supervisor.config.repo_path, "done": h.task.done() if h.task else False}
            for rid, h in manager.runs.items()
        ]

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: str) -> dict[str, Any]:
        h = manager.get(run_id)
        return {
            "run_id": run_id,
            "phase": h.supervisor.phase,
            "summary": h.supervisor.summary.model_dump(mode="json"),
            "transitions": [t.__dict__ for t in h.supervisor.fsm.history],
            "keys": h.supervisor.bb.keys(),
            "awaiting_approval": h.supervisor.phase == Phase.STRANGLER and not h.supervisor.ctx.approval.is_set(),
        }

    @app.post("/api/runs/{run_id}/approve")
    async def approve(run_id: str) -> dict[str, str]:
        manager.get(run_id).supervisor.approve()
        return {"status": "approved"}

    @app.get("/api/runs/{run_id}/state/{key}")
    async def state(run_id: str, key: str) -> Any:
        bb = manager.get(run_id).supervisor.bb
        if not bb.has(key):
            raise HTTPException(404, f"key {key} not on blackboard yet")
        value = bb.get(key)
        return value.model_dump(mode="json", by_alias=True) if isinstance(value, BaseModel) else value

    @app.get("/api/runs/{run_id}/graph")
    async def graph(run_id: str) -> dict[str, Any]:
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

    @app.get("/api/runs/{run_id}/events")
    async def events(run_id: str) -> StreamingResponse:
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

    @app.get("/api/agents")
    async def agents() -> list[dict[str, str]]:
        from reposplit.core.registry import describe_agents

        return describe_agents()

    return app
