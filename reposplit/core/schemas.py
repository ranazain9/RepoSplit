"""Shared Pydantic contracts.

Every artifact that crosses an agent boundary is one of these models and lives on the
Blackboard under a well-known key (see `Keys`). Agents never pass raw dicts to each other.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


def utcnow_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


# --------------------------------------------------------------------------------------
# Run lifecycle
# --------------------------------------------------------------------------------------


class Phase(StrEnum):
    INGEST = "INGEST"
    ARCHITECT = "ARCHITECT"
    DATA = "DATA"
    CONTRACT = "CONTRACT"
    STRANGLER = "STRANGLER"
    SCAFFOLD = "SCAFFOLD"
    PARITY = "PARITY"
    GOVERNANCE = "GOVERNANCE"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"


class RiskLevel(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class Keys:
    """Blackboard key registry. Keep every cross-agent key here so `requires`/`produces` are auditable."""

    RUN_CONFIG = "run.config"
    REPO_MANIFEST = "repo.manifest"
    DEPENDENCY_GRAPH = "architect.dependency_graph"
    TOPOLOGY = "architect.topology"
    DATA_PLAN = "data.partition_plan"
    CONTRACT_PLAN = "contract.plan"
    GATEWAY_PLAN = "strangler.gateway_plan"
    SCAFFOLD_MANIFEST = "scaffold.manifest"
    PARITY_REPORT = "parity.report"
    PASSPORT = "governance.passport"
    FINOPS_REPORT = "governance.finops_report"
    ARTIFACTS = "run.artifacts"
    PROMPTS = "run.prompts"
    APPROVALS = "run.approvals"


class RunConfig(BaseModel):
    repo_path: str
    output_dir: str = "out"
    provider: Literal["auto", "mock", "anthropic", "watsonx", "groq"] = "auto"
    model: str | None = None
    mode: Literal["full", "strangler"] = "full"
    target_services: list[str] = Field(default_factory=list, description="strangler mode: clusters to extract")
    canary_weight: int = Field(10, ge=0, le=100)
    monolith_url: str | None = None
    services_url: str | None = Field(None, description="gateway base URL; routes are matched to services by path")
    live: bool = Field(False, description="boot monolith + generated services locally and run live parity")
    auto_heal: bool = True
    max_heal_iterations: int = 3
    strict_parity: bool = Field(False, description="fail the run if parity regressions remain after healing")
    require_approval: bool = False
    deploy: bool = False
    seed: int = 42
    strict_llm: bool = False
    signing_key_path: str | None = None
    uuid_refs: bool = Field(False, description="Sever FKs to String(36) UUID refs instead of keeping the column type")
    topology_override: str | None = Field(None, description="Path to JSON file with manual symbol/file -> cluster overrides")
    manual_assignments: dict[str, str] = Field(default_factory=dict, description="In-memory symbol/file -> cluster overrides")
    gateway: Literal["envoy", "kong", "both"] = Field("envoy", description="Gateway config to generate: envoy, kong, or both")


class TelemetryEvent(BaseModel):
    ts: str = Field(default_factory=utcnow_iso)
    seq: int = 0
    phase: Phase
    agent: str
    level: Literal["debug", "info", "warning", "error", "success"] = "info"
    message: str
    payload: dict[str, Any] = Field(default_factory=dict)


class PromptRecord(BaseModel):
    agent: str
    provider: str
    model: str
    sha256: str
    ts: str = Field(default_factory=utcnow_iso)
    used_default: bool = False


class AgentResult(BaseModel):
    agent: str
    phase: Phase
    ok: bool
    summary: str
    produced: list[str] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    duration_s: float = 0.0


class RunSummary(BaseModel):
    run_id: str
    status: Phase
    started_at: str
    finished_at: str | None = None
    results: list[AgentResult] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)
    error: str | None = None


class RepoManifest(BaseModel):
    root: str
    file_count: int
    python_files: int
    total_loc: int
    tree_sha256: str
    languages: dict[str, int]


# --------------------------------------------------------------------------------------
# Phase 1 - Architect
# --------------------------------------------------------------------------------------


class ParamSpec(BaseModel):
    name: str
    type_hint: str | None = None
    required: bool = True
    default: str | None = None


class RouteInfo(BaseModel):
    path: str
    methods: list[str] = Field(default_factory=lambda: ["GET"])
    blueprint: str | None = None
    response_fields: list[str] = Field(default_factory=list, description="Response body fields extracted from return AST")
    status_codes: list[int] = Field(default_factory=list, description="HTTP status codes returned")
    operation_kind: str = Field("READ", description="READ | MUTATING | IDEMPOTENT")


class GraphNode(BaseModel):
    id: str
    kind: Literal["module", "class", "function", "method"]
    module: str
    name: str
    lineno: int = 0
    end_lineno: int = 0
    loc: int = 0
    params: list[ParamSpec] = Field(default_factory=list)
    returns: str | None = None
    route: RouteInfo | None = None
    is_model: bool = False
    table: str | None = Field(None, description="__tablename__ for ORM model classes")
    bases: list[str] = Field(default_factory=list)
    decorators: list[str] = Field(default_factory=list)
    body_keys: list[str] = Field(default_factory=list, description="JSON body keys read via request.get_json()")


class GraphEdge(BaseModel):
    source: str
    target: str
    kind: Literal["import", "call", "data_access", "inherits", "fk", "transaction"]
    weight: int = 1
    in_loop: bool = False


class DependencyGraph(BaseModel):
    root: str
    language: str = "python"
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    file_count: int = 0
    total_loc: int = 0

    def node_map(self) -> dict[str, GraphNode]:
        return {n.id: n for n in self.nodes}


class CouplingMetrics(BaseModel):
    ca: int = Field(0, description="Afferent coupling: incoming cross-boundary edge weight")
    ce: int = Field(0, description="Efferent coupling: outgoing cross-boundary edge weight")
    instability: float = Field(0.0, ge=0.0, le=1.0, description="I = Ce / (Ca + Ce)")


class Cluster(BaseModel):
    name: str
    kind: Literal["service", "shared_kernel"] = "service"
    files: list[str] = Field(default_factory=list)
    symbols: list[str] = Field(default_factory=list)
    metrics: CouplingMetrics = Field(default_factory=CouplingMetrics)
    loc: int = 0
    extract: bool = True
    rationale: str = ""


class SeveredEdge(BaseModel):
    source: str
    target: str
    source_cluster: str
    target_cluster: str
    kind: str
    weight: int
    risk: RiskLevel
    in_loop: bool = False
    chatty_warning: str | None = None


class DependencyCycle(BaseModel):
    members: list[str]
    resolution: str


class DomainTopology(BaseModel):
    clusters: dict[str, Cluster]
    severed_edges: list[SeveredEdge]
    cycles: list[DependencyCycle] = Field(default_factory=list)
    split_files: list[str] = Field(default_factory=list, description="files whose symbols landed in >1 cluster")
    modularity: float = 0.0
    initial_coupling: float = 0.0
    severed_coupling: float = 0.0
    risk_level: RiskLevel = RiskLevel.LOW
    rationale: str = ""
    human_override: bool = False

    def service_clusters(self) -> dict[str, Cluster]:
        return {k: v for k, v in self.clusters.items() if v.kind == "service" and v.extract}

    def cluster_of(self, symbol: str) -> str | None:
        for name, c in self.clusters.items():
            if symbol in c.symbols:
                return name
        return None


class TopologyPreviewRequest(BaseModel):
    assignments: dict[str, str] = Field(..., description="Map of symbol or module -> target cluster name")


class TopologyPreviewResponse(BaseModel):
    clusters: dict[str, list[str]]
    initial_coupling: float
    severed_coupling: float
    coupling_reduction_pct: float
    severed_edges_count: int
    risk_level: RiskLevel
    cycles_count: int
    cycles: list[DependencyCycle] = Field(default_factory=list)


class ClusterAssignment(BaseModel):
    """What the LLM is allowed to change about a heuristic cluster: its name and rationale."""

    heuristic_name: str
    proposed_name: str
    rationale: str


class ArchitectDecision(BaseModel):
    clusters: list[ClusterAssignment]
    high_risk_cut_points: list[str] = Field(default_factory=list)
    risk_level: RiskLevel = RiskLevel.LOW
    rationale: str = ""


# --------------------------------------------------------------------------------------
# Phase 2 - Data / DataSplit
# --------------------------------------------------------------------------------------


class ColumnSpec(BaseModel):
    name: str
    type_expr: str
    primary_key: bool = False
    nullable: bool = True
    foreign_key: str | None = Field(None, description="'table.column' if this column carries a FK")
    ondelete: str | None = Field(None, description="CASCADE, SET NULL, RESTRICT, etc.")
    source: str = Field("", description="original source line")


class EntityModel(BaseModel):
    class_name: str
    table: str
    module: str
    symbol: str
    lineno: int = 0
    end_lineno: int = 0
    columns: list[ColumnSpec]
    relationships: list[str] = Field(default_factory=list)


class ForeignKeySeverance(BaseModel):
    table: str
    column: str
    references: str
    owner_service: str
    references_service: str
    before: str
    after: str
    migration_sql: str
    cascade_delete: bool = False
    risk: RiskLevel = RiskLevel.MEDIUM


class SagaStep(BaseModel):
    order: int
    name: str
    service: str
    action: str
    compensation: str | None = None
    compensation_exists: bool = False


class SagaDefinition(BaseModel):
    name: str
    trigger: str
    orchestrator_service: str
    source_symbol: str
    steps: list[SagaStep]
    rationale: str = ""


class CQRSProjection(BaseModel):
    name: str
    read_model: str
    source_symbol: str
    owner_service: str
    source_tables: list[str]
    source_services: list[str]
    store: Literal["redis", "postgres_view"] = "redis"
    refresh: Literal["outbox_event", "scheduled"] = "outbox_event"


class DataPartitionPlan(BaseModel):
    service_schemas: dict[str, list[str]]
    entities: list[EntityModel]
    severed_foreign_keys: list[ForeignKeySeverance]
    sagas: list[SagaDefinition]
    projections: list[CQRSProjection]
    shared_tables: list[str] = Field(default_factory=list)
    god_files: list[str] = Field(default_factory=list, description="model files split across >1 service")
    rationale: str = ""


class SagaReview(BaseModel):
    saga_name: str
    approved: bool = True
    reordered_steps: list[str] = Field(default_factory=list)
    missing_compensations: list[str] = Field(default_factory=list)
    notes: str = ""


class DataDecision(BaseModel):
    saga_reviews: list[SagaReview] = Field(default_factory=list)
    risky_severances: list[str] = Field(default_factory=list)
    rationale: str = ""


# --------------------------------------------------------------------------------------
# Phase 3 - Contract
# --------------------------------------------------------------------------------------


class EndpointContract(BaseModel):
    service: str
    operation_id: str
    method: str
    path: str
    exposure: Literal["public", "internal"]
    params: list[ParamSpec] = Field(default_factory=list)
    body_keys: list[str] = Field(default_factory=list)
    path_params: list[str] = Field(default_factory=list)
    response_type: str = "object"
    response_fields: list[str] = Field(default_factory=list)
    status_codes: list[int] = Field(default_factory=list)
    source_symbol: str
    callers: list[str] = Field(default_factory=list)


class ServiceContract(BaseModel):
    service: str
    port: int
    base_path: str
    endpoints: list[EndpointContract]
    downstream: list[str] = Field(default_factory=list, description="services this one calls")
    openapi: dict[str, Any] = Field(default_factory=dict)
    proto: str = ""


class ContractPlan(BaseModel):
    services: dict[str, ServiceContract]
    propagated_headers: list[str] = Field(default_factory=lambda: ["X-User-Id", "X-Tenant-Id", "traceparent"])
    rationale: str = ""


# --------------------------------------------------------------------------------------
# Phase 4 - Strangler Fig
# --------------------------------------------------------------------------------------


class GatewayRoute(BaseModel):
    path: str
    method: str
    service: str
    canary_weight: int
    monolith_weight: int


class CanaryStage(BaseModel):
    stage: int
    canary_weight: int
    bake_minutes: int
    success_criteria: str


class GatewayPlan(BaseModel):
    gateway: Literal["envoy", "kong"] = "envoy"
    listen_port: int = 8080
    monolith_upstream: str = "monolith:5000"
    routes: list[GatewayRoute]
    stages: list[CanaryStage]
    rollback_error_rate_threshold: float = 0.001
    rationale: str = ""


# --------------------------------------------------------------------------------------
# Scaffold (Supervisor-driven step between STRANGLER and PARITY)
# --------------------------------------------------------------------------------------


class ScaffoldedService(BaseModel):
    service: str
    directory: str
    port: int
    files: list[str]
    routes: int
    rpcs: int
    porting_notes: list[str] = Field(default_factory=list)


class ScaffoldManifest(BaseModel):
    services: dict[str, ScaffoldedService]
    compose_file: str
    helm_chart: str
    monolith_context: str


# --------------------------------------------------------------------------------------
# Phase 5 - Test & Parity
# --------------------------------------------------------------------------------------


class ParityCase(BaseModel):
    id: str
    service: str
    method: str
    path: str
    payload: dict[str, Any] | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    monolith_status: int | None = None
    service_status: int | None = None
    monolith_body: Any = None
    service_body: Any = None
    status: Literal["PASS", "FAIL", "PENDING", "HEALED", "ERROR"] = "PENDING"
    diff: list[str] = Field(default_factory=list)
    latency_ms: dict[str, float] = Field(default_factory=dict)


class HealPatch(BaseModel):
    case_id: str
    file: str
    find: str
    replace: str
    rationale: str
    confidence: float = Field(0.5, ge=0, le=1)


class HealDecision(BaseModel):
    patches: list[HealPatch] = Field(default_factory=list)
    needs_human: list[str] = Field(default_factory=list)
    rationale: str = ""


class ParityReport(BaseModel):
    mode: Literal["live", "suite_generated"]
    total: int
    passed: int
    failed: int
    pending: int
    errors: int = 0
    pass_rate: float
    healing_iterations: int = 0
    applied_patches: list[HealPatch] = Field(default_factory=list)
    normalization_masks: list[str]
    cases: list[ParityCase]


# --------------------------------------------------------------------------------------
# Phase 6 - Governance / Migration Passport
# --------------------------------------------------------------------------------------


class Subject(BaseModel):
    name: str
    digest: dict[str, str]


class InTotoStatement(BaseModel):
    type_: str = Field("https://in-toto.io/Statement/v1", alias="_type")
    subject: list[Subject]
    predicateType: str = "https://reposplit.ai/attestation/v2"
    predicate: dict[str, Any]

    model_config = {"populate_by_name": True}


class DSSESignature(BaseModel):
    keyid: str
    sig: str


class DSSEEnvelope(BaseModel):
    payloadType: str = "application/vnd.in-toto+json"
    payload: str
    signatures: list[DSSESignature]


class MigrationPassport(BaseModel):
    statement: InTotoStatement
    envelope: DSSEEnvelope
    public_key_pem: str
    signer_did: str


# --------------------------------------------------------------------------------------
# FinOps Cloud Cost & ROI
# --------------------------------------------------------------------------------------


class CostBreakdown(BaseModel):
    compute_monthly_usd: float
    database_monthly_usd: float
    ingress_gateway_monthly_usd: float
    total_monthly_usd: float


class FinOpsReport(BaseModel):
    currency: str = "USD"
    monolith_baseline: CostBreakdown
    modernized_fleet: CostBreakdown
    monthly_savings_usd: float
    annual_savings_usd: float
    savings_percent: float
    carbon_reduction_kg_yr: float
    roi_multiple: float
    assumptions: list[str] = Field(default_factory=list)

