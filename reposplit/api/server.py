"""Telemetry API + dashboard host.

Modular FastAPI architecture following enterprise market conventions.
Routers:
  - /api/upload, git clone -> reposplit.api.routes.ingestion
  - /api/runs/*            -> reposplit.api.routes.runs
  - /api/* (download)      -> reposplit.api.routes.delivery
  - /api/topology/*        -> reposplit.api.routes.topology
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from reposplit.api.config import settings
from reposplit.api.models import INDEX_FILE, STATIC, RunHandle, RunManager, StartRun
from reposplit.api.routes import delivery, ingestion, runs, topology
from reposplit.api.routes.ingestion import _clone_git_repo, _safe_extract_zip, is_git_url

__all__ = [
    "INDEX_FILE",
    "STATIC",
    "RunHandle",
    "RunManager",
    "StartRun",
    "_clone_git_repo",
    "_safe_extract_zip",
    "app",
    "create_app",
    "is_git_url",
    "settings",
]


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.title,
        version=settings.version,
    )

    # Enterprise CORS configuration
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # In-memory run state manager
    app.state.manager = RunManager()

    # Mount domain routers
    app.include_router(ingestion.router)
    app.include_router(runs.router)
    app.include_router(delivery.router)
    app.include_router(topology.router)

    from starlette.staticfiles import StaticFiles

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    # Reference client UI
    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(
            STATIC / "index.html",
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )

    return app


app = create_app()
