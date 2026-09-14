"""Test & Parity Agent - Phase 5: differential testing with an auto-healing feedback loop."""

from __future__ import annotations

import asyncio
from pathlib import Path

from reposplit.agents.parity.healer import apply_patches, heal_context, heuristic_decision
from reposplit.agents.parity.local_stack import local_stack
from reposplit.agents.parity.prompts import HEAL_SYSTEM, heal_user_prompt
from reposplit.agents.parity.runner import DifferentialRunner, Targets
from reposplit.agents.parity.semantic_diff import DEFAULT_MASKS
from reposplit.agents.parity.suite import build_suite, load_traffic
from reposplit.core.base_agent import BaseAgent
from reposplit.core.schemas import (
    AgentResult,
    ContractPlan,
    HealDecision,
    Keys,
    ParityCase,
    ParityReport,
    Phase,
    ScaffoldManifest,
)


class ParityAgent(BaseAgent):
    name = "parity"
    phase = Phase.PARITY
    requires = (Keys.CONTRACT_PLAN, Keys.SCAFFOLD_MANIFEST)
    produces = (Keys.PARITY_REPORT,)
    uses_llm = True
    description = "Synthesizes/replays traffic against monolith and services, semantic JSON diff, auto-heal patches."

    async def execute(self) -> AgentResult:
        contracts = self.bb.require(Keys.CONTRACT_PLAN, ContractPlan)
        manifest = self.bb.require(Keys.SCAFFOLD_MANIFEST, ScaffoldManifest)
        previous = self.bb.get(Keys.PARITY_REPORT, ParityReport)
        iteration = (previous.healing_iterations + 1) if previous else 0

        traffic = Path(self.config.output_dir) / "traffic.jsonl"
        cases = load_traffic(traffic, contracts) if traffic.exists() else build_suite(contracts, seed=1)
        self.write_artifact("reports/parity_suite.json", [c.model_dump(mode="json") for c in cases])
        self.log(f"{len(cases)} differential case(s) ready ({'replayed' if traffic.exists() else 'synthesized'})")

        mode = "live" if (self.config.monolith_url and (self.config.services_url or self.config.live)) or self.config.live else "suite_generated"
        if mode == "live":
            # blocking HTTP + subprocess work off the event loop so SSE telemetry keeps streaming
            cases = await asyncio.to_thread(self._run_live, cases, contracts)
        else:
            self.log("no targets: emitting suite in PENDING state (use --live or --monolith-url/--services-url)", level="warning")

        applied = []
        decision: HealDecision | None = None
        failed = [c for c in cases if c.status == "FAIL"]
        if mode == "live" and failed and self.config.auto_heal and iteration < self.config.max_heal_iterations:
            decision = await self._diagnose(failed, manifest)
            applied = apply_patches(decision.patches, self.ctx.output_dir, manifest)
            self.bb.put(Keys.SCAFFOLD_MANIFEST, manifest)
            for case in failed:
                if any(p.case_id == case.id for p in applied):
                    case.status = "HEALED"
            self.log(
                f"auto-heal: {len(applied)} patch(es) applied, {len(decision.needs_human)} case(s) need a human",
                level="warning" if decision.needs_human else "info",
                patches=[p.model_dump(mode="json") for p in applied],
                needs_human=decision.needs_human,
            )

        report = self._report(cases, mode, iteration, applied)
        self.bb.put(Keys.PARITY_REPORT, report)
        self.write_artifact("reports/parity_report.json", report)
        if decision and decision.needs_human:
            self.write_artifact("reports/parity_needs_human.md", "# Parity cases needing a human\n\n" + "\n".join(f"- {n}" for n in decision.needs_human) + "\n")
        summary = (
            f"{report.passed}/{report.total} parity ({report.pass_rate:.0%}), {report.failed} failed, {report.errors} errors"
            if mode == "live"
            else f"{report.total} parity case(s) generated (PENDING - no live targets)"
        )
        for w in [f"{c.id}: {'; '.join(c.diff[:3])}" for c in cases if c.status in ("FAIL", "ERROR")][:10]:
            self.warn(w)
        return self.ok(summary, mode=mode, pass_rate=report.pass_rate, failed=report.failed, healed=len(applied), iteration=iteration)

    # ---- live run ------------------------------------------------------------------

    def _run_live(self, cases: list[ParityCase], contracts: ContractPlan) -> list[ParityCase]:
        def on_case(c: ParityCase) -> None:
            self.log(f"{c.status:6} {c.id} {c.method} {c.path} [{c.monolith_status}/{c.service_status}]", level="debug" if c.status == "PASS" else "warning", diff=c.diff[:5])

        if self.config.monolith_url and self.config.services_url:
            targets = Targets(monolith_url=self.config.monolith_url, gateway_url=self.config.services_url)
            runner = DifferentialRunner(targets, DEFAULT_MASKS)
            try:
                return runner.run(cases, on_case)
            finally:
                runner.close()
        self.log("booting local stack (monolith + generated services) for live parity")
        with local_stack(self.ctx.repo_root, self.ctx.output_dir, contracts, self.ctx.output_dir / ".reposplit" / "logs") as (mono, urls):
            self.log("stack healthy", monolith=mono, services=urls)
            runner = DifferentialRunner(Targets(monolith_url=mono, service_urls=urls), DEFAULT_MASKS)
            try:
                return runner.run(cases, on_case)
            finally:
                runner.close()

    # ---- healing -------------------------------------------------------------------

    async def _diagnose(self, failed: list[ParityCase], manifest: ScaffoldManifest) -> HealDecision:
        default = heuristic_decision(failed, manifest, self.ctx.output_dir)
        decision = await self.decide(
            system=HEAL_SYSTEM,
            user=heal_user_prompt(heal_context(failed, self.ctx.output_dir)),
            schema=HealDecision,
            default=default,
        )
        # Never let the model touch anything but ported service code.
        decision.patches = [p for p in decision.patches if p.file.startswith("services/") and p.file.endswith("main.py")]
        return decision

    @staticmethod
    def _report(cases: list[ParityCase], mode: str, iteration: int, applied) -> ParityReport:
        passed = sum(1 for c in cases if c.status == "PASS")
        failed = sum(1 for c in cases if c.status in ("FAIL", "HEALED"))
        errors = sum(1 for c in cases if c.status == "ERROR")
        pending = sum(1 for c in cases if c.status == "PENDING")
        total = len(cases)
        return ParityReport(
            mode=mode,  # type: ignore[arg-type]
            total=total,
            passed=passed,
            failed=failed,
            pending=pending,
            errors=errors,
            pass_rate=(passed / total) if total and mode == "live" else 0.0,
            healing_iterations=iteration,
            applied_patches=applied,
            normalization_masks=DEFAULT_MASKS,
            cases=cases,
        )
