"""Governance & Passport Agent - Phase 6: cryptographic attestation of the whole run.

Everything that influenced the transformation is hashed into the predicate: the source tree, the
cut decision graph, every prompt (and whether the LLM or the deterministic default decided), the
model/provider, and the parity verification. The statement is DSSE-signed with Ed25519 so a
compliance reviewer can verify it offline with `reposplit verify`.
"""

from __future__ import annotations

from pathlib import Path

from reposplit import ENGINE_NAME, __version__
from reposplit.agents.governance.attestation import load_or_create_key, sign_statement, verify_passport
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
from reposplit.utils.hashing import hash_file, hash_tree, sha256_json


class GovernanceAgent(BaseAgent):
    name = "governance"
    phase = Phase.GOVERNANCE
    requires = (Keys.REPO_MANIFEST, Keys.TOPOLOGY, Keys.DATA_PLAN, Keys.PARITY_REPORT, Keys.SCAFFOLD_MANIFEST)
    produces = (Keys.PASSPORT,)
    uses_llm = False
    description = "SHA-256 provenance of source, cut graph, prompts and parity; in-toto v1 + DSSE Ed25519 Migration Passport."

    async def execute(self) -> AgentResult:
        manifest = self.bb.require(Keys.REPO_MANIFEST, RepoManifest)
        topology = self.bb.require(Keys.TOPOLOGY, DomainTopology)
        data_plan = self.bb.require(Keys.DATA_PLAN, DataPartitionPlan)
        parity = self.bb.require(Keys.PARITY_REPORT, ParityReport)
        scaffold = self.bb.require(Keys.SCAFFOLD_MANIFEST, ScaffoldManifest)
        out = self.ctx.output_dir

        subjects = [Subject(name="services", digest={"sha256": hash_tree(out / "services")})]
        for rel in ("reports/domain_topology.json", "reports/data_partition_plan.json", "reports/parity_report.json", "gateway/envoy.yaml"):
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
            "humanOversight": {
                "approvalGateEnabled": self.config.require_approval,
                "portingNotesOpen": sum(len(s.porting_notes) for s in scaffold.services.values()),
            },
            "complianceControls": {
                "EU_AI_ACT_ART14_HUMAN_OVERSIGHT": "approval gate before scaffolding; every LLM decision hashed and attributable",
                "EU_AI_ACT_ART12_RECORD_KEEPING": "full telemetry event log + blackboard snapshot persisted",
                "SLSA_PROVENANCE": "in-toto v1 statement, DSSE envelope, Ed25519 signature",
            },
        }
        statement = InTotoStatement(subject=subjects, predicate=predicate)

        key_path = Path(self.config.signing_key_path) if self.config.signing_key_path else None
        key, used = load_or_create_key(key_path, out / "keys")
        passport = sign_statement(statement, key)
        ok, problems = verify_passport(passport)
        if not ok:
            raise AgentError(f"self-verification of passport failed: {problems}")
        self.bb.put(Keys.PASSPORT, passport)
        self.write_artifact("reports/migration_passport.json", passport)
        self.bb.register_artifact("keys/attestation_key.pub")
        return self.ok(
            f"Migration Passport signed ({passport.signer_did[:24]}...), {len(subjects)} subject(s), {len(prompts)} prompt hash(es)",
            signer=passport.signer_did,
            key_path=str(used),
            subjects=[s.name for s in subjects],
            llm_decisions=len(llm_decisions),
        )
