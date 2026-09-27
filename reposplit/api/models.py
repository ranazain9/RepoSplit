"""Shared models, state managers, and data types for RepoSplit API."""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import HTTPException
from pydantic import BaseModel, Field

from reposplit.core.schemas import RunConfig
from reposplit.core.supervisor import Supervisor

STATIC = Path(__file__).parent / "static"
INDEX_FILE = Path(".reposplit") / "runs_index.json"


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
        output_dir = req.output_dir or "out"
        config = RunConfig(
            repo_path=req.repo_path,
            output_dir=output_dir,
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

        # Persist run index
        self._record_run(run_id, req.repo_path, output_dir)
        return handle

    def _record_run(self, run_id: str, repo: str, output_dir: str) -> None:
        try:
            index = {}
            if INDEX_FILE.exists():
                index = json.loads(INDEX_FILE.read_text(encoding="utf-8"))
            index[run_id] = {"repo": repo, "output_dir": output_dir, "run_id": run_id}
            INDEX_FILE.parent.mkdir(parents=True, exist_ok=True)
            INDEX_FILE.write_text(json.dumps(index, indent=2), encoding="utf-8")
        except Exception:
            pass

    def get(self, run_id: str) -> RunHandle:
        if run_id not in self.runs:
            raise HTTPException(404, f"unknown run {run_id}")
        return self.runs[run_id]
