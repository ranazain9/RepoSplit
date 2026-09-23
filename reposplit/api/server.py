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
from pydantic import BaseModel, Field

from reposplit.agents.architect.graph_metrics import build_domain_topology
from reposplit.core.schemas import (
    DependencyGraph,
    DomainTopology,
    Keys,
    Phase,
    RunConfig,
    TopologyPreviewRequest,
    TopologyPreviewResponse,
)
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
    manual_assignments: dict[str, str] = Field(default_factory=dict)
    topology_override: str | None = None


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
            output_dir=req.output_dir or "out",
            provider=req.provider,  # type: ignore[arg-type]
            mode=req.mode,  # type: ignore[arg-type]
            target_services=req.target_services,
            live=req.live,
            require_approval=req.require_approval,
            canary_weight=req.canary_weight,
            manual_assignments=req.manual_assignments,
            topology_override=req.topology_override,
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

    @app.get("/api/certificate")
    async def certificate() -> FileResponse:
        path = Path("out/reports/migration_certificate.html")
        if not path.exists():
            raise HTTPException(404, "certificate not generated yet")
        return FileResponse(path)

    @app.get("/api/latest")
    async def latest_run() -> dict[str, Any]:
        import json
        reports = Path("out/reports")
        passport_file = reports / "migration_passport.json"
        if not passport_file.exists():
            return {"has_run": False}
        passport = json.loads(passport_file.read_text(encoding="utf-8"))
        finops_file = reports / "finops_report.json"
        finops = json.loads(finops_file.read_text(encoding="utf-8")) if finops_file.exists() else None
        pred = passport.get("statement", {}).get("predicate", {})
        return {
            "has_run": True,
            "run_id": pred.get("runId", "latest"),
            "parity": pred.get("parityTestVerification", {}),
            "finops": finops,
            "signer": passport.get("signer_did"),
            "services": pred.get("cutDecisionGraph", {}).get("services", []),
        }

    @app.get("/api/latest/graph")
    async def latest_graph() -> dict[str, Any]:
        import json
        reports = Path("out/reports")
        graph_file = reports / "dependency_graph.json"
        if not graph_file.exists():
            raise HTTPException(404, "no graph found on disk")
        g = json.loads(graph_file.read_text(encoding="utf-8"))
        topo_file = reports / "domain_topology.json"
        topo = json.loads(topo_file.read_text(encoding="utf-8")) if topo_file.exists() else {}
        cluster_of = {}
        for name, c in topo.get("clusters", {}).items():
            for s in c.get("symbols", []):
                cluster_of[s] = name
        nodes = [
            {
                "id": n["id"],
                "name": n["name"],
                "kind": n["kind"],
                "module": n["module"],
                "cluster": cluster_of.get(n["id"], "unassigned"),
                "is_model": n.get("is_model", False),
                "route": n.get("route", {}).get("path") if n.get("route") else None,
            }
            for n in g.get("nodes", [])
            if n.get("kind") in ("class", "function")
        ]
        ids = {n["id"] for n in nodes}
        edges = [e for e in g.get("edges", []) if e.get("source") in ids and e.get("target") in ids]
        severed = {(s["source"], s["target"]) for s in topo.get("severed_edges", [])}
        for e in edges:
            e["severed"] = (e.get("source"), e.get("target")) in severed
        return {"nodes": nodes, "edges": edges, "clusters": sorted(set(cluster_of.values()))}

    @app.get("/api/topology")
    async def get_topology() -> dict[str, Any]:
        import json

        reports = Path("out/reports")
        topo_file = reports / "domain_topology.json"
        graph_file = reports / "dependency_graph.json"
        if not topo_file.exists():
            raise HTTPException(404, "domain topology not generated yet")
        topo = json.loads(topo_file.read_text(encoding="utf-8"))
        symbols_info = []
        if graph_file.exists():
            g = json.loads(graph_file.read_text(encoding="utf-8"))
            for n in g.get("nodes", []):
                if n.get("kind") in ("class", "function"):
                    symbols_info.append({
                        "id": n["id"],
                        "name": n["name"],
                        "kind": n["kind"],
                        "module": n["module"],
                        "loc": n.get("loc", 0),
                        "is_model": n.get("is_model", False),
                    })
        return {"topology": topo, "symbols": symbols_info}

    @app.post("/api/topology/preview")
    async def preview_topology(req: TopologyPreviewRequest) -> TopologyPreviewResponse:
        import json

        reports = Path("out/reports")
        graph_file = reports / "dependency_graph.json"
        if not graph_file.exists():
            raise HTTPException(404, "dependency graph not found on disk")
        g_data = json.loads(graph_file.read_text(encoding="utf-8"))
        graph = DependencyGraph.model_validate(g_data)

        # Baseline assignment:
        topo_file = reports / "domain_topology.json"
        current_assignment: dict[str, str] = {}
        if topo_file.exists():
            topo_data = json.loads(topo_file.read_text(encoding="utf-8"))
            for cname, cl in topo_data.get("clusters", {}).items():
                for s in cl.get("symbols", []):
                    current_assignment[s] = cname

        # Apply overrides from req:
        nodes = graph.node_map()
        for sid, node in nodes.items():
            if sid in req.assignments:
                current_assignment[sid] = req.assignments[sid]
            elif node.name in req.assignments:
                current_assignment[sid] = req.assignments[node.name]
            elif node.module in req.assignments:
                current_assignment[sid] = req.assignments[node.module]

        new_topo = build_domain_topology(graph, current_assignment, human_override=True)
        clusters_map = {name: c.symbols for name, c in new_topo.clusters.items()}
        reduction = (
            round((1.0 - (new_topo.severed_coupling / new_topo.initial_coupling)) * 100.0, 1)
            if new_topo.initial_coupling > 0
            else 0.0
        )
        return TopologyPreviewResponse(
            clusters=clusters_map,
            initial_coupling=new_topo.initial_coupling,
            severed_coupling=new_topo.severed_coupling,
            coupling_reduction_pct=reduction,
            severed_edges_count=len(new_topo.severed_edges),
            risk_level=new_topo.risk_level,
            cycles_count=len(new_topo.cycles),
            cycles=new_topo.cycles,
        )

    @app.post("/api/topology/save")
    async def save_topology_override(req: TopologyPreviewRequest) -> dict[str, Any]:
        import json

        out_dir = Path("out")
        out_dir.mkdir(parents=True, exist_ok=True)
        override_file = out_dir / "custom_topology.json"
        override_file.write_text(json.dumps(req.assignments, indent=2), encoding="utf-8")
        return {"status": "saved", "path": str(override_file), "rules_count": len(req.assignments)}

    return app


app = create_app()
