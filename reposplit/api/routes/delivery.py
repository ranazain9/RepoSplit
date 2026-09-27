"""Fleet artifact delivery, zip packaging, passport reports, and migration certificates."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from reposplit.api.config import settings
from reposplit.api.models import RunManager

router = APIRouter(prefix="/api", tags=["delivery"])


def _manager(request: Request) -> RunManager:
    return request.app.state.manager


@router.get("/runs/{run_id}/download")
async def download_fleet(run_id: str, request: Request) -> FileResponse:
    manager = _manager(request)
    h = manager.get(run_id)
    out_dir = Path(h.supervisor.config.output_dir)
    if not out_dir.exists():
        raise HTTPException(404, "Run output directory does not exist")

    zip_dest = settings.storage_dir / "downloads" / f"reposplit-fleet-{run_id}.zip"
    zip_dest.parent.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(zip_dest, "w", zipfile.ZIP_DEFLATED) as z:
        for file_path in out_dir.rglob("*"):
            if file_path.is_file():
                arcname = file_path.relative_to(out_dir)
                z.write(file_path, arcname)

    return FileResponse(
        zip_dest,
        media_type="application/zip",
        filename=f"reposplit-modernized-{run_id}.zip",
    )


@router.get("/latest/download")
async def download_latest() -> FileResponse:
    out_dir = Path("out")
    if not out_dir.exists():
        raise HTTPException(404, "No modernized output found on disk")
    zip_dest = settings.storage_dir / "downloads" / "reposplit-fleet-latest.zip"
    zip_dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_dest, "w", zipfile.ZIP_DEFLATED) as z:
        for file_path in out_dir.rglob("*"):
            if file_path.is_file():
                arcname = file_path.relative_to(out_dir)
                z.write(file_path, arcname)
    return FileResponse(
        zip_dest,
        media_type="application/zip",
        filename="reposplit-modernized-latest.zip",
    )


@router.get("/certificate")
async def certificate() -> FileResponse:
    path = Path("out/reports/migration_certificate.html")
    if not path.exists():
        raise HTTPException(404, "certificate not generated yet")
    return FileResponse(path)


@router.get("/latest")
async def latest_run() -> dict[str, Any]:
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


@router.get("/latest/graph")
async def latest_graph() -> dict[str, Any]:
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
