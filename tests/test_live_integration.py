"""Integration: boot the monolith and the generated services as subprocesses and run live parity.

Proves the vertical slice end to end: AST -> partition -> severed schemas -> contracts -> ported
FastAPI services -> differential test across real HTTP boundaries -> signed passport.
Set REPOSPLIT_SKIP_LIVE=1 to skip (e.g. on machines without free localhost ports).
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from reposplit.agents.governance.attestation import load_passport, verify_passport
from reposplit.core.schemas import Keys, ParityReport, Phase, RunConfig
from reposplit.core.supervisor import Supervisor
from reposplit.llm.mock_provider import MockProvider

pytestmark = pytest.mark.skipif(os.environ.get("REPOSPLIT_SKIP_LIVE") == "1", reason="live stack disabled")


def test_live_parity_across_generated_services(monolith_path: Path, tmp_path: Path) -> None:
    pytest.importorskip("flask")
    config = RunConfig(repo_path=str(monolith_path), output_dir=str(tmp_path / "out"), provider="mock", live=True)
    supervisor = Supervisor(config, provider=MockProvider())
    summary = asyncio.run(supervisor.run())
    assert summary.status == Phase.COMPLETE, summary.error

    report = supervisor.bb.require(Keys.PARITY_REPORT, ParityReport)
    assert report.mode == "live" and report.errors == 0
    assert report.pass_rate >= 0.8, [c.id for c in report.cases if c.status != "PASS"]
    # The only known gap is the cross-service JOIN, which the porter flags and the healer routes to a human.
    failed = [c for c in report.cases if c.status == "FAIL"]
    assert all("order_history" in c.id for c in failed)
    by_id = {c.id: c for c in report.cases}
    create = next(c for k, c in by_id.items() if k.endswith("_create_order"))
    assert create.monolith_status == create.service_status == 201
    assert create.monolith_body["total"] == create.service_body["total"]  # tax logic survived the port
    cancel = next(c for k, c in by_id.items() if k.endswith("_cancel_order"))
    assert cancel.service_body["status"] == "cancelled"  # saga compensations ran across services

    passport = load_passport(tmp_path / "out" / "reports" / "migration_passport.json")
    assert verify_passport(passport)[0]
    assert passport.statement.predicate["parityTestVerification"]["mode"] == "live"
