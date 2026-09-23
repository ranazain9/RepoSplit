"""Supervisor Orchestrator Agent.

Owns the execution DAG (an explicit FSM), the phase gates, the human-approval checkpoint, the
auto-healing loop, and Blackboard persistence. It never does domain work itself - it only
sequences the specialised agents and validates what they put on the Blackboard.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from pathlib import Path

from reposplit import ENGINE_NAME, __version__
from reposplit.core.base_agent import AgentContext, BaseAgent
from reposplit.core.blackboard import Blackboard
from reposplit.core.fsm import PhaseMachine
from reposplit.core.schemas import (
    AgentResult,
    ContractPlan,
    DataPartitionPlan,
    DomainTopology,
    Keys,
    ParityReport,
    Phase,
    RepoManifest,
    RunConfig,
    RunSummary,
    utcnow_iso,
)
from reposplit.llm.provider import LLMProvider, build_provider
from reposplit.utils.hashing import hash_tree, iter_repo_files

SUPERVISOR = "supervisor"

# Phase gates: each returns a list of violations; a non-empty list fails the run.
GateFn = Callable[[Blackboard], list[str]]


def _gate_architect(bb: Blackboard) -> list[str]:
    topo = bb.require(Keys.TOPOLOGY, DomainTopology)
    problems: list[str] = []
    if not topo.service_clusters():
        problems.append("Architect produced no extractable service clusters")
    if topo.severed_coupling > 0.95:
        problems.append(f"severed coupling {topo.severed_coupling:.2f} > 0.95: partition is meaningless")
    return problems


def _gate_data(bb: Blackboard) -> list[str]:
    plan = bb.require(Keys.DATA_PLAN, DataPartitionPlan)
    return [] if plan.service_schemas or not plan.entities else ["Data agent found entities but assigned none"]


def _gate_contract(bb: Blackboard) -> list[str]:
    plan = bb.require(Keys.CONTRACT_PLAN, ContractPlan)
    return [] if plan.services else ["Contract agent produced no service contracts"]


def _gate_parity(bb: Blackboard) -> list[str]:
    report = bb.require(Keys.PARITY_REPORT, ParityReport)
    config = bb.require(Keys.RUN_CONFIG, RunConfig)
    if report.mode == "live" and report.failed > 0 and config.strict_parity:
        return [f"parity regressions remain after healing: {report.failed} failed (strict_parity)"]
    if report.mode == "live" and report.errors == report.total and report.total:
        return ["every parity case errored: services or monolith unreachable"]
    return []


PHASE_GATES: dict[Phase, GateFn] = {
    Phase.ARCHITECT: _gate_architect,
    Phase.DATA: _gate_data,
    Phase.CONTRACT: _gate_contract,
    Phase.PARITY: _gate_parity,
}


class Supervisor:
    def __init__(
        self,
        config: RunConfig,
        *,
        provider: LLMProvider | None = None,
        blackboard: Blackboard | None = None,
        run_id: str | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.config = config
        self.run_id = run_id or uuid.uuid4().hex[:12]
        self.output_dir = Path(config.output_dir).resolve()
        self.repo_root = Path(config.repo_path).resolve()
        self.bb = blackboard or Blackboard(self.run_id, self.output_dir)
        self.provider = provider or build_provider(config.provider, config.model)
        self.fsm = PhaseMachine()
        self.ctx = AgentContext(
            config=config,
            llm=self.provider,
            blackboard=self.bb,
            output_dir=self.output_dir,
            repo_root=self.repo_root,
            logger=logger or logging.getLogger("reposplit"),
        )
        self.results: list[AgentResult] = []
        self.summary = RunSummary(run_id=self.run_id, status=Phase.INGEST, started_at=utcnow_iso())
        if not config.require_approval:
            self.ctx.approval.set()

    # ---- public API ----------------------------------------------------------------

    def approve(self) -> None:
        """Human oversight hook (EU AI Act Art. 14): unblocks the SCAFFOLD phase."""
        self.bb.record(self.fsm.state, SUPERVISOR, "human approval received", level="success")
        self.ctx.approval.set()

    @classmethod
    def from_snapshot(cls, snapshot_path: str | Path, config: RunConfig) -> Supervisor:
        """Reconstruct a Supervisor from a saved blackboard snapshot.

        Advances the FSM to the phase after the last completed one so the run
        continues from where it left off. Called by the CLI when --resume is set.
        """
        from pathlib import Path as _Path

        from reposplit.core.blackboard import Blackboard
        from reposplit.core.schemas import Keys

        snapshot_path = _Path(snapshot_path)
        bb = Blackboard.load(snapshot_path)

        supervisor = cls(config, blackboard=bb, run_id=bb.run_id)

        # Determine which phases are complete by checking which keys exist on the blackboard
        phase_keys = [
            (Phase.ARCHITECT, Keys.TOPOLOGY),
            (Phase.DATA, Keys.DATA_PLAN),
            (Phase.CONTRACT, Keys.CONTRACT_PLAN),
            (Phase.STRANGLER, Keys.GATEWAY_PLAN),
            (Phase.SCAFFOLD, Keys.SCAFFOLD_MANIFEST),
            (Phase.PARITY, Keys.PARITY_REPORT),
            (Phase.GOVERNANCE, Keys.PASSPORT),
        ]
        last_done = Phase.INGEST
        for phase, key in phase_keys:
            if bb.has(key):
                last_done = phase
            else:
                break

        # Advance the FSM to the last completed phase so the pipeline can pick up from the next
        if last_done != Phase.INGEST:
            import contextlib
            with contextlib.suppress(Exception):
                supervisor.fsm.advance(last_done, f"restored from snapshot (last complete: {last_done})")

        # If the approval gate was already passed, set the event so we don't block
        if bb.has(Keys.SCAFFOLD_MANIFEST) or not config.require_approval:
            supervisor.ctx.approval.set()

        supervisor._log(
            f"run resumed from snapshot (last completed: {last_done})",
            level="info",
            snapshot=str(snapshot_path),
        )
        return supervisor

    @property
    def phase(self) -> Phase:
        return self.fsm.state

    def build_pipeline(self) -> list[BaseAgent]:
        from reposplit.core.registry import default_pipeline

        return default_pipeline(self.ctx)

    async def run(self) -> RunSummary:
        self._log(f"{ENGINE_NAME} v{__version__} run {self.run_id} starting", repo=str(self.repo_root))
        try:
            await self._ingest()
            for agent in self.build_pipeline():
                if agent.phase == Phase.SCAFFOLD:
                    await self._wait_for_approval()
                if agent.phase == Phase.PARITY:
                    await self._run_parity_with_healing(agent)
                    continue
                await self._run_agent(agent)
            self.fsm.advance(Phase.COMPLETE, "all phases passed")
            self.summary.status = Phase.COMPLETE
            self._log("modernization complete", level="success", artifacts=len(self.bb.artifacts()))
        except Exception as exc:  # noqa: BLE001 - the supervisor is the last line of defence
            self.fsm.fail(str(exc))
            self.summary.status = Phase.FAILED
            self.summary.error = str(exc)
            self._log(f"run failed: {exc}", level="error")
        finally:
            self.summary.finished_at = utcnow_iso()
            self.summary.results = self.results
            self.summary.artifacts = self.bb.artifacts()
            self._persist()
        return self.summary

    # ---- phases --------------------------------------------------------------------

    async def _ingest(self) -> None:
        if not self.repo_root.is_dir():
            raise FileNotFoundError(f"repo path does not exist: {self.repo_root}")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        files = iter_repo_files(self.repo_root)
        languages: dict[str, int] = {}
        total_loc = 0
        for f in files:
            languages[f.suffix or "<none>"] = languages.get(f.suffix or "<none>", 0) + 1
            if f.suffix == ".py":
                total_loc += sum(1 for _ in f.open("r", encoding="utf-8", errors="ignore"))
        manifest = RepoManifest(
            root=str(self.repo_root),
            file_count=len(files),
            python_files=languages.get(".py", 0),
            total_loc=total_loc,
            tree_sha256=hash_tree(self.repo_root),
            languages=languages,
        )
        self.bb.put(Keys.RUN_CONFIG, self.config)
        self.bb.put(Keys.REPO_MANIFEST, manifest)
        self._log(
            f"ingested {manifest.python_files} python files / {manifest.total_loc} LOC",
            tree_sha256=manifest.tree_sha256,
        )
        if manifest.python_files == 0:
            raise RuntimeError("no Python files found - only Python monoliths are supported in this scaffold")
        self._persist()

    async def _run_agent(self, agent: BaseAgent) -> AgentResult:
        self.fsm.advance(agent.phase, f"start {agent.name}")
        self.summary.status = agent.phase
        result = await agent.run()
        self.results.append(result)
        if not result.ok:
            raise RuntimeError(f"{agent.name} failed: {result.summary}")
        violations = PHASE_GATES.get(agent.phase, lambda _bb: [])(self.bb)
        if violations:
            raise RuntimeError(f"phase gate {agent.phase} rejected: {violations}")
        self._log(f"phase gate {agent.phase} passed", level="debug")
        self._persist()
        return result

    async def _run_parity_with_healing(self, parity_agent: BaseAgent) -> None:
        """PARITY -> (SCAFFOLD -> PARITY)* until the report is clean or the iteration budget is spent."""
        from reposplit.core.registry import scaffold_agent

        self.fsm.advance(Phase.PARITY, "start parity")
        self.summary.status = Phase.PARITY
        iteration = 0
        while True:
            result = await parity_agent.run()
            self.results.append(result)
            if not result.ok:
                raise RuntimeError(f"{parity_agent.name} failed: {result.summary}")
            report = self.bb.require(Keys.PARITY_REPORT, ParityReport)
            patches = report.applied_patches
            clean = report.mode != "live" or report.failed == 0
            if clean or not self.config.auto_heal or iteration >= self.config.max_heal_iterations or not patches:
                break
            iteration += 1
            self._log(f"auto-heal iteration {iteration}: re-scaffolding {len(patches)} patched file(s)")
            self.fsm.advance(Phase.SCAFFOLD, "auto-heal re-scaffold")
            await scaffold_agent(self.ctx).run()
            self.fsm.advance(Phase.PARITY, "auto-heal re-test")
        violations = _gate_parity(self.bb)
        if violations:
            raise RuntimeError(f"phase gate PARITY rejected: {violations}")
        self._persist()

    async def _wait_for_approval(self) -> None:
        if self.ctx.approval.is_set():
            return
        self._log(
            "awaiting human approval before scaffolding services (POST /api/runs/{id}/approve or --yes)",
            level="warning",
            approval_required=True,
        )
        await asyncio.wait_for(self.ctx.approval.wait(), timeout=None)

    # ---- misc ----------------------------------------------------------------------

    def _log(self, message: str, level: str = "info", **payload) -> None:
        self.bb.record(self.fsm.state, SUPERVISOR, message, level=level, **payload)
        self.ctx.logger.info("[supervisor] %s", message)

    def _persist(self) -> None:
        self.bb.save()
        target = self.output_dir / ".reposplit" / "run_summary.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.summary.model_dump_json(indent=2), encoding="utf-8")
