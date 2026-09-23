"""Data Agent / DataSplit Engine - Phase 2: relational state decoupling."""

from __future__ import annotations

import ast
import re
from collections import defaultdict

from reposplit.agents.data.prompts import DATA_SYSTEM, data_user_prompt
from reposplit.agents.data.schema_parser import parse_entities, sever_column_source
from reposplit.core.base_agent import BaseAgent
from reposplit.core.schemas import (
    AgentResult,
    CQRSProjection,
    DataDecision,
    DataPartitionPlan,
    DependencyGraph,
    DomainTopology,
    EntityModel,
    ForeignKeySeverance,
    Keys,
    Phase,
    RiskLevel,
    SagaDefinition,
    SagaReview,
    SagaStep,
)
from reposplit.generators.render import render
from reposplit.utils.naming import to_pascal

WRITE_VERBS = ("create", "update", "delete", "cancel", "reserve", "charge", "place", "submit", "register", "add")
COMPENSATION_LEXICON: dict[str, str] = {
    "reserve": "release",
    "charge": "refund",
    "create": "delete",
    "add": "remove",
    "increment": "decrement",
    "lock": "unlock",
    "allocate": "deallocate",
    "hold": "release",
    "debit": "credit",
    "reserve_stock": "release_stock",
}
COMPENSATING_VERBS = set(COMPENSATION_LEXICON.values()) | {"rollback", "cancel", "undo", "compensate"}
READ_VERBS = {"find", "get", "list", "fetch", "load", "read", "lookup", "search", "compute", "calculate", "validate"}

SQLA_IMPORT = (
    "from sqlalchemy import BigInteger, Boolean, Column, Date, DateTime, Enum, Float, ForeignKey, Integer, JSON, Numeric, SmallInteger, String, Text, Time\n"
    "from sqlalchemy.orm import declarative_base, relationship\n\n"
    "Base = declarative_base()\n"
)


class DataAgent(BaseAgent):
    name = "data"
    phase = Phase.DATA
    requires = (Keys.DEPENDENCY_GRAPH, Keys.TOPOLOGY)
    produces = (Keys.DATA_PLAN,)
    uses_llm = True
    description = "Entity ownership, foreign-key severing, Saga/Outbox write paths, CQRS read projections."

    async def execute(self) -> AgentResult:
        graph = self.bb.require(Keys.DEPENDENCY_GRAPH, DependencyGraph)
        topology = self.bb.require(Keys.TOPOLOGY, DomainTopology)
        entities = parse_entities(self.ctx.repo_root, graph)
        self.log(f"parsed {len(entities)} ORM entities", entities=[e.table for e in entities])

        owner_of_table: dict[str, str] = {}
        service_schemas: dict[str, list[str]] = defaultdict(list)
        shared_tables: list[str] = []
        for ent in entities:
            owner = topology.cluster_of(ent.symbol)
            if owner is None or topology.clusters[owner].kind != "service":
                shared_tables.append(ent.table)
                continue
            owner_of_table[ent.table] = owner
            service_schemas[owner].append(ent.table)

        severed = self._sever_foreign_keys(entities, owner_of_table)
        sagas = self._synthesize_sagas(graph, topology)
        projections = self._synthesize_projections(graph, topology, entities)
        god_files = sorted(
            {e.module for e in entities if len({owner_of_table.get(x.table) for x in entities if x.module == e.module}) > 1}
        )

        plan = DataPartitionPlan(
            service_schemas=dict(sorted(service_schemas.items())),
            entities=entities,
            severed_foreign_keys=severed,
            sagas=sagas,
            projections=projections,
            shared_tables=shared_tables,
            god_files=god_files,
            rationale="Tables follow the service that owns their model class; cross-service FKs become soft "
            "references (constraint dropped, column kept); cross-service writes become Sagas; cross-service reads "
            "become CQRS projections fed by outbox events.",
        )
        plan = await self._review_with_llm(plan)
        self.bb.put(Keys.DATA_PLAN, plan)

        self._write_isolated_schemas(plan, owner_of_table, entities)
        self._write_migration(plan)
        self.write_artifact("data/saga_orchestrator.py", render("saga_orchestrator.py.j2", sagas=plan.sagas))
        self.write_artifact("data/cqrs_views.py", render("cqrs_views.py.j2", projections=plan.projections))
        self.write_artifact("data/outbox.py", render("outbox.py.j2", service="shared"))
        self.write_artifact("reports/data_partition_plan.json", plan)

        return self.ok(
            f"{len(plan.service_schemas)} schemas, {len(severed)} FK(s) severed, {len(sagas)} saga(s), "
            f"{len(projections)} CQRS projection(s)",
            schemas={k: len(v) for k, v in plan.service_schemas.items()},
            severed_foreign_keys=len(severed),
            sagas=[s.name for s in sagas],
            projections=[p.name for p in projections],
            god_files=god_files,
        )

    # ---- foreign keys -------------------------------------------------------------

    def _sever_foreign_keys(self, entities: list[EntityModel], owner_of_table: dict[str, str]) -> list[ForeignKeySeverance]:
        severed: list[ForeignKeySeverance] = []
        for ent in entities:
            owner = owner_of_table.get(ent.table)
            if owner is None:
                continue
            for col in ent.columns:
                if not col.foreign_key:
                    continue
                ref_table = col.foreign_key.split(".")[0]
                ref_owner = owner_of_table.get(ref_table)
                if ref_owner is None or ref_owner == owner:
                    continue
                is_cascade = bool(col.ondelete and col.ondelete.upper() == "CASCADE")
                after = sever_column_source(col.source, self.config.uuid_refs)
                after += f"  # soft reference -> {ref_owner}.{col.foreign_key}"
                if is_cascade:
                    after += " [CASCADE DELETED REMOVED: requires domain event handler or saga]"
                sql = [f"ALTER TABLE {ent.table} DROP CONSTRAINT IF EXISTS {ent.table}_{col.name}_fkey;"]
                if is_cascade:
                    sql.append(f"-- WARNING: On-delete CASCADE severed across boundary ({ent.table} -> {col.foreign_key}).")
                    sql.append(f"-- Application must subscribe to '{ref_owner}.deleted' events or invoke compensating saga.")
                if self.config.uuid_refs:
                    sql.append(f"ALTER TABLE {ent.table} ALTER COLUMN {col.name} TYPE VARCHAR(36);")
                sql.append(f"CREATE INDEX IF NOT EXISTS ix_{ent.table}_{col.name} ON {ent.table} ({col.name});")
                severed.append(
                    ForeignKeySeverance(
                        table=ent.table,
                        column=col.name,
                        references=col.foreign_key,
                        owner_service=owner,
                        references_service=ref_owner,
                        before=col.source,
                        after=after,
                        migration_sql="\n".join(sql),
                        cascade_delete=is_cascade,
                        risk=RiskLevel.CRITICAL if is_cascade else (RiskLevel.HIGH if not col.nullable else RiskLevel.MEDIUM),
                    )
                )
        return severed

    # ---- sagas ----------------------------------------------------------------------

    def _synthesize_sagas(self, graph: DependencyGraph, topology: DomainTopology) -> list[SagaDefinition]:
        nodes = graph.node_map()
        cross_calls: dict[str, dict[str, str]] = defaultdict(dict)  # fn -> {callee name: target cluster}
        for s in topology.severed_edges:
            if s.kind == "call":
                cross_calls[s.source][nodes[s.target].name] = s.target_cluster
        sagas: list[SagaDefinition] = []
        for fn_id, callees in sorted(cross_calls.items()):
            fn = nodes[fn_id]
            if not self._is_write(fn.name, fn.route.methods if fn.route else []):
                continue
            owner = topology.cluster_of(fn_id) or "unknown"
            ordered = self._call_order(fn_id, list(callees))
            steps: list[SagaStep] = []
            # Reads (find_user, get_product) are preconditions, compensations are rollback paths: neither is a step.
            forward_names = {c for c in ordered if not self._is_compensating(c) and not self._is_read(c)}
            for callee in ordered:
                if callee not in forward_names:
                    continue
                comp = self._compensation_for(callee, callees[callee], topology, nodes)
                steps.append(
                    SagaStep(
                        order=len(steps) + 1,
                        name=to_pascal(callee),
                        service=callees[callee],
                        action=callee,
                        compensation=comp[0],
                        compensation_exists=comp[1],
                    )
                )
            if not steps:
                continue
            steps.append(
                SagaStep(
                    order=len(steps) + 1,
                    name=f"Finalize{to_pascal(fn.name)}",
                    service=owner,
                    action=fn.name,
                    compensation=f"mark_{fn.name}_failed",
                    compensation_exists=False,
                )
            )
            sagas.append(
                SagaDefinition(
                    name=f"{to_pascal(fn.name)}Saga",
                    trigger=f"{to_pascal(fn.name)}Command",
                    orchestrator_service=owner,
                    source_symbol=fn_id,
                    steps=steps,
                    rationale=f"{fn.name} writes across {len(forward_names) + 1} services; replaced by a compensable saga.",
                )
            )
        return sagas

    @staticmethod
    def _is_write(name: str, methods: list[str]) -> bool:
        if any(m in ("POST", "PUT", "PATCH", "DELETE") for m in methods):
            return True
        return name.split("_")[0] in WRITE_VERBS

    @staticmethod
    def _is_compensating(name: str) -> bool:
        return name.split("_")[0] in COMPENSATING_VERBS

    @staticmethod
    def _is_read(name: str) -> bool:
        return name.split("_")[0] in READ_VERBS

    def _call_order(self, fn_id: str, callees: list[str]) -> list[str]:
        module, fname = fn_id.split("::")
        source = (self.ctx.repo_root / module).read_text(encoding="utf-8", errors="ignore")
        tree = ast.parse(source)
        calls: list[tuple[int, int, str]] = []
        for node in tree.body:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == fname:
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Call):
                        cname = ast.unparse(sub.func).split(".")[-1]
                        if cname in callees:
                            calls.append((sub.lineno, sub.col_offset, cname))
        order: list[str] = []
        for _, _, cname in sorted(calls):  # source order, not ast.walk (BFS) order
            if cname not in order:
                order.append(cname)
        return order + [c for c in callees if c not in order]

    @staticmethod
    def _compensation_for(callee: str, service: str, topology: DomainTopology, nodes) -> tuple[str | None, bool]:
        available = {nodes[s].name for s in topology.clusters[service].symbols if s in nodes}
        if callee in COMPENSATION_LEXICON and COMPENSATION_LEXICON[callee] in available:
            return COMPENSATION_LEXICON[callee], True
        verb, _, rest = callee.partition("_")
        if verb in COMPENSATION_LEXICON:
            candidate = f"{COMPENSATION_LEXICON[verb]}_{rest}" if rest else COMPENSATION_LEXICON[verb]
            return candidate, candidate in available
        return f"compensate_{callee}", False

    # ---- projections ---------------------------------------------------------------

    def _synthesize_projections(
        self, graph: DependencyGraph, topology: DomainTopology, entities: list[EntityModel]
    ) -> list[CQRSProjection]:
        nodes = graph.node_map()
        table_of = {e.symbol: e.table for e in entities}
        reads: dict[str, set[str]] = defaultdict(set)  # fn -> remote model symbols
        for s in topology.severed_edges:
            if s.kind == "data_access":
                reads[s.source].add(s.target)
        projections: list[CQRSProjection] = []
        for fn_id, remote in sorted(reads.items()):
            fn = nodes[fn_id]
            if self._is_write(fn.name, fn.route.methods if fn.route else []):
                continue  # write paths that touch foreign data are handled as sagas / client calls
            owner = topology.cluster_of(fn_id) or "unknown"
            local = {e.target for e in graph.edges if e.source == fn_id and e.kind == "data_access"} - remote
            tables = sorted({table_of[s] for s in local | remote if s in table_of})
            services = sorted({topology.cluster_of(s) or "unknown" for s in remote})
            projections.append(
                CQRSProjection(
                    name=f"{to_pascal(fn.name)}View",
                    read_model=f"{fn.name}_view",
                    source_symbol=fn_id,
                    owner_service=owner,
                    source_tables=tables,
                    source_services=services,
                )
            )
        return projections

    # ---- LLM review ---------------------------------------------------------------

    async def _review_with_llm(self, plan: DataPartitionPlan) -> DataPartitionPlan:
        default = DataDecision(
            saga_reviews=[
                SagaReview(
                    saga_name=s.name,
                    approved=True,
                    missing_compensations=[st.compensation or st.name for st in s.steps if not st.compensation_exists],
                )
                for s in plan.sagas
            ],
            risky_severances=[f"{f.table}.{f.column}" for f in plan.severed_foreign_keys if f.risk == RiskLevel.HIGH],
            rationale=plan.rationale,
        )
        decision = await self.decide(
            system=DATA_SYSTEM, user=data_user_prompt(self._summary(plan)), schema=DataDecision, default=default
        )
        by_name = {s.name: s for s in plan.sagas}
        for review in decision.saga_reviews:
            saga = by_name.get(review.saga_name)
            if saga is None:
                continue
            if review.notes:
                saga.rationale = review.notes
            if not review.approved:
                self.warn(f"LLM did not approve saga {saga.name}: {review.notes}")
            if review.reordered_steps:
                order = {name: i for i, name in enumerate(review.reordered_steps)}
                if set(order) == {st.name for st in saga.steps}:
                    saga.steps.sort(key=lambda st: order[st.name])
                    for i, st in enumerate(saga.steps, start=1):
                        st.order = i
        for col in decision.risky_severances:
            for f in plan.severed_foreign_keys:
                if f"{f.table}.{f.column}" == col:
                    f.risk = RiskLevel.HIGH
        if decision.rationale:
            plan.rationale = decision.rationale
        return plan

    @staticmethod
    def _summary(plan: DataPartitionPlan) -> str:
        lines = ["service schemas:"]
        for svc, tables in plan.service_schemas.items():
            lines.append(f"- {svc}: {', '.join(tables)}")
        lines.append("severed foreign keys:")
        for f in plan.severed_foreign_keys:
            cascade_flag = " [CASCADE]" if f.cascade_delete else ""
            lines.append(f"- {f.table}.{f.column} -> {f.references} ({f.owner_service} -> {f.references_service}, risk {f.risk}{cascade_flag})")
        lines.append("sagas:")
        for s in plan.sagas:
            steps = " -> ".join(f"{st.name}@{st.service}[comp={st.compensation}{'' if st.compensation_exists else '?'}]" for st in s.steps)
            lines.append(f"- {s.name} ({s.trigger}): {steps}")
        lines.append("projections:")
        for p in plan.projections:
            lines.append(f"- {p.name} in {p.owner_service} from {p.source_tables} via {p.source_services}")
        return "\n".join(lines)

    # ---- artifacts -----------------------------------------------------------------

    def _write_isolated_schemas(
        self, plan: DataPartitionPlan, owner_of_table: dict[str, str], entities: list[EntityModel]
    ) -> None:
        severed_by_line = {(f.table, f.before.strip()): f.after for f in plan.severed_foreign_keys}
        owner_of_class = {e.class_name: owner_of_table.get(e.table) for e in entities}
        for service, tables in plan.service_schemas.items():
            parts = [f'"""Isolated schema for {service} - generated by RepoSplit DataSplit Engine."""\n', SQLA_IMPORT]
            for ent in entities:
                if ent.table in tables:
                    parts.append(self._rewrite_model(ent, severed_by_line, owner_of_class, service))
            self.write_artifact(f"data/isolated_schemas/{service}/models.py", "\n".join(parts))

    def _rewrite_model(
        self, ent: EntityModel, severed: dict[tuple[str, str], str], owner_of_class: dict[str, str | None], service: str
    ) -> str:
        """Flask-SQLAlchemy class -> plain SQLAlchemy declarative, with severed FKs and cross-service relationships."""
        source = (self.ctx.repo_root / ent.module).read_text(encoding="utf-8", errors="ignore").splitlines()
        block = source[ent.lineno - 1 : ent.end_lineno]
        out: list[str] = []
        for line in block:
            stripped = line.strip()
            indent = line[: len(line) - len(line.lstrip())]
            if (ent.table, stripped) in severed:
                out.append(indent + severed[(ent.table, stripped)])
                continue
            if "relationship(" in stripped:
                m = re.search(r"relationship\((['\"])([^'\"]+)\1", stripped)
                target = m.group(2) if m else None
                target_owner = owner_of_class.get(target or "")
                if target and target_owner not in (None, service):
                    out.append(f"{indent}# {stripped}  # removed: cross-service relationship -> {target_owner}")
                    continue
            out.append(line)
        text = "\n".join(out)
        text = re.sub(r"\((?:db\.)?Model\)", "(Base)", text, count=1)
        text = re.sub(r"\bdb\.", "", text)
        return text + "\n"

    def _write_migration(self, plan: DataPartitionPlan) -> None:
        lines = ["-- RepoSplit DataSplit Engine: sever cross-service foreign keys (phase 1: drop constraints, keep columns)", ""]
        for f in plan.severed_foreign_keys:
            lines.append(f"-- {f.table}.{f.column} -> {f.references} ({f.owner_service} -> {f.references_service})")
            lines.append(f.migration_sql)
            lines.append("")
        lines.append("-- Phase 2 (per service): create the isolated database and copy the owned tables.")
        for svc, tables in plan.service_schemas.items():
            lines.append(f"-- {svc}: {', '.join(tables)}")
        self.write_artifact("data/migrations/001_sever_foreign_keys.sql", "\n".join(lines) + "\n")
