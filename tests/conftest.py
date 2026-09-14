from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reposplit.core.schemas import RunConfig, RunSummary
from reposplit.core.supervisor import Supervisor
from reposplit.llm.mock_provider import MockProvider

ROOT = Path(__file__).resolve().parent.parent
MONOLITH = ROOT / "examples" / "shop_monolith"


@pytest.fixture(scope="session")
def monolith_path() -> Path:
    return MONOLITH


@pytest.fixture(scope="session")
def pipeline_run(tmp_path_factory: pytest.TempPathFactory) -> tuple[Supervisor, RunSummary]:
    """One full offline (non-live) run shared by the e2e tests."""
    out = tmp_path_factory.mktemp("out")
    config = RunConfig(repo_path=str(MONOLITH), output_dir=str(out), provider="mock", require_approval=False)
    supervisor = Supervisor(config, provider=MockProvider())
    summary = asyncio.run(supervisor.run())
    return supervisor, summary
