"""Governance & Passport Agent - Phase 6: cryptographic attestation of the whole run.

Everything that influenced the transformation is hashed into the predicate: the source tree, the
cut decision graph, every prompt (and whether the LLM or the deterministic default decided), the
model/provider, and the parity verification. The statement is DSSE-signed with Ed25519 so a
compliance reviewer can verify it offline with `reposplit verify`.
"""

from __future__ import annotations

from pathlib import Path

from reposplit import ENGINE_NAME, __version__
from reposplit.agents.governance.attestation import (
    did_key,
    load_or_create_key,
    sign_statement,
    verify_passport,
)
from reposplit.core.base_agent import AgentError, BaseAgent
from reposplit.core.schemas import (
    AgentResult,
    DataPartitionPlan,
    DomainTopology,
    InTotoStatement,
    Keys,
    ParityReport,
    Phase,
    RepoManifest,
    ScaffoldManifest,
    Subject,
    utcnow_iso,
)
from reposplit.generators.render import render
from reposplit.utils.finops import calculate_finops
from reposplit.utils.hashing import hash_file, hash_tree, sha256_json


class GovernanceAgent(BaseAgent):
    name = "governance"
    phase = Phase.GOVERNANCE
    requires = (Keys.REPO_MANIFEST, Keys.TOPOLOGY, Keys.DATA_PLAN, Keys.PARITY_REPORT, Keys.SCAFFOLD_MANIFEST)
    produces = (Keys.PASSPORT, Keys.FINOPS_REPORT)
    uses_llm = False
    description = "SHA-256 provenance of source, cut graph, prompts and parity; in-toto v1 + DSSE Ed25519 Migration Passport."

    async def execute(self) -> AgentResult:
        manifest = self.bb.require(Keys.REPO_MANIFEST, RepoManifest)
        topology = self.bb.require(Keys.TOPOLOGY, DomainTopology)
        data_plan = self.bb.require(Keys.DATA_PLAN, DataPartitionPlan)
        parity = self.bb.require(Keys.PARITY_REPORT, ParityReport)
        scaffold = self.bb.require(Keys.SCAFFOLD_MANIFEST, ScaffoldManifest)
        out = self.ctx.output_dir

        finops = calculate_finops(
            total_loc=manifest.total_loc,
            service_count=len(topology.service_clusters()),
            route_count=sum(s.routes for s in scaffold.services.values()),
            db_count=len(topology.service_clusters()),
        )
        self.bb.put(Keys.FINOPS_REPORT, finops)
        self.write_artifact("reports/finops_report.json", finops)

        key_path = Path(self.config.signing_key_path) if self.config.signing_key_path else None
        key, used = load_or_create_key(key_path, out / "keys")
        signer = did_key(key.public_key())

        cert_html = render(
            "migration_certificate.html.j2",
            run_id=self.bb.run_id,
            engine=ENGINE_NAME,
            version=__version__,
            timestamp=utcnow_iso(),
            signer_did=signer,
            manifest=manifest,
            topology=topology,
            data_plan=data_plan,
            parity=parity,
            finops=finops,
            scaffold=scaffold,
        )
        self.write_artifact("reports/migration_certificate.html", cert_html)

        subjects = [Subject(name="services", digest={"sha256": hash_tree(out / "services")})]
        for tree_rel in ("helm/reposplit", "data/isolated_schemas"):
            tree_path = out / tree_rel
            if tree_path.exists():
                subjects.append(Subject(name=tree_rel, digest={"sha256": hash_tree(tree_path)}))
        for rel in (
            "docker-compose.yml",
            "gateway/envoy.yaml",
            "reports/domain_topology.json",
            "reports/data_partition_plan.json",
            "reports/contract_plan.json",
            "reports/scaffold_manifest.json",
            "reports/parity_suite.json",
            "reports/parity_report.json",
            "reports/finops_report.json",
            "reports/migration_certificate.html",
        ):
            path = out / rel
            if path.exists():
                subjects.append(Subject(name=rel, digest={"sha256": hash_file(path)}))

        prompts = self.bb.prompts()
        llm_decisions = [p for p in prompts if not p.used_default]
        predicate = {
            "modernizationEngine": ENGINE_NAME,
            "engineVersion": __version__,
            "timestamp": utcnow_iso(),
            "runId": self.bb.run_id,
            "source": {"root": manifest.root, "treeSha256": manifest.tree_sha256, "pythonFiles": manifest.python_files, "loc": manifest.total_loc},
            "cutDecisionGraph": {
                "sha256": sha256_json(topology.model_dump(mode="json")),
                "services": sorted(topology.service_clusters()),
                "severedEdges": len(topology.severed_edges),
                "riskLevel": topology.risk_level,
                "humanOverride": topology.human_override,
            },
            "astCouplingScore": {"initialCoupling": topology.initial_coupling, "severedCoupling": topology.severed_coupling, "modularity": topology.modularity},
            "dataSplit": {
                "severedForeignKeys": len(data_plan.severed_foreign_keys),
                "sagas": [s.name for s in data_plan.sagas],
                "cqrsProjections": [p.name for p in data_plan.projections],
                "sha256": sha256_json(data_plan.model_dump(mode="json")),
            },
            "modelProvenance": {
                "provider": self.ctx.llm.name,
                "model": self.ctx.llm.model,
                "prompts": [p.model_dump(mode="json") for p in prompts],
                "llmDecisions": len(llm_decisions),
                "deterministicDecisions": len(prompts) - len(llm_decisions),
            },
            "parityTestVerification": {
                "mode": parity.mode,
                "totalTests": parity.total,
                "passed": parity.passed,
                "failed": parity.failed,
                "passRate": f"{parity.pass_rate:.0%}" if parity.mode == "live" else "not executed",
                "healingIterations": parity.healing_iterations,
                "sha256": sha256_json(parity.model_dump(mode="json")),
            },
            "finOpsRoi": {
                "monolithMonthlyUsd": finops.monolith_baseline.total_monthly_usd,
                "monolithComputeMonthlyUsd": finops.monolith_baseline.compute_monthly_usd,
                "monolithDbMonthlyUsd": finops.monolith_baseline.database_monthly_usd,
                "monolithGatewayMonthlyUsd": finops.monolith_baseline.ingress_gateway_monthly_usd,
                "microservicesMonthlyUsd": finops.modernized_fleet.total_monthly_usd,
                "microservicesComputeMonthlyUsd": finops.modernized_fleet.compute_monthly_usd,
                "microservicesDbMonthlyUsd": finops.modernized_fleet.database_monthly_usd,
                "microservicesGatewayMonthlyUsd": finops.modernized_fleet.ingress_gateway_monthly_usd,
                "monthlySavingsUsd": finops.monthly_savings_usd,
                "annualSavingsUsd": finops.annual_savings_usd,
                "savingsPercent": f"{finops.savings_percent:.1f}%",
                "carbonReductionKgYr": finops.carbon_reduction_kg_yr,
                "roiMultiple": f"{finops.roi_multiple:.1f}x",
                "sha256": sha256_json(finops.model_dump(mode="json")),
            },
            "humanOversight": {
                "approvalGateEnabled": self.config.require_approval,
                "humanTopologyOverrideApplied": topology.human_override,
                "portingNotesOpen": sum(len(s.porting_notes) for s in scaffold.services.values()),
            },
            "complianceControls": {
                "EU_AI_ACT_ART14_HUMAN_OVERSIGHT": "approval gate before scaffolding; every LLM decision hashed and attributable",
                "EU_AI_ACT_ART12_RECORD_KEEPING": "full telemetry event log + blackboard snapshot persisted",
                "SLSA_PROVENANCE": "in-toto v1 statement, DSSE envelope, Ed25519 signature",
            },
        }
        statement = InTotoStatement(subject=subjects, predicate=predicate)

        passport = sign_statement(statement, key)
        ok, problems = verify_passport(passport)
        if not ok:
            raise AgentError(f"self-verification of passport failed: {problems}")
        self.bb.put(Keys.PASSPORT, passport)
        self.write_artifact("reports/migration_passport.json", passport)
        self.bb.register_artifact("keys/attestation_key.pub")
        return self.ok(
            f"Migration Passport signed ({passport.signer_did[:24]}...), {len(subjects)} subject(s), "
            f"{finops.savings_percent:.0f}% FinOps savings (${finops.annual_savings_usd:,.0f}/yr)",
            signer=passport.signer_did,
            subjects=len(subjects),
            prompts=len(prompts),
            annual_savings_usd=finops.annual_savings_usd,
            savings_percent=finops.savings_percent,
            key_path=str(used),
            llm_decisions=len(llm_decisions),
        )

