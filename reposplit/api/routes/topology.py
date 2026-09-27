"""Topology endpoints: domain cluster inspection and interactive boundary tuning."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

from reposplit.agents.architect.graph_metrics import build_domain_topology
from reposplit.core.schemas import (
    DependencyGraph,
    TopologyPreviewRequest,
    TopologyPreviewResponse,
)

router = APIRouter(prefix="/api", tags=["topology"])


@router.get("/topology")
async def get_topology() -> dict[str, Any]:
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


@router.post("/topology/preview")
async def preview_topology(req: TopologyPreviewRequest) -> TopologyPreviewResponse:
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


@router.post("/topology/save")
async def save_topology_override(req: TopologyPreviewRequest) -> dict[str, Any]:
    out_dir = Path("out")
    out_dir.mkdir(parents=True, exist_ok=True)
    override_file = out_dir / "custom_topology.json"
    override_file.write_text(json.dumps(req.assignments, indent=2), encoding="utf-8")
    return {"status": "saved", "path": str(override_file), "rules_count": len(req.assignments)}
