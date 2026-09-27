"""Architect Agent - Phase 1: whole-system AST comprehension and domain partitioning."""

from __future__ import annotations

import re
from pathlib import Path

from reposplit.agents.architect.ast_parser import PythonRepoParser
from reposplit.agents.architect.graph_metrics import (
    SHARED_KERNEL,
    build_domain_topology,
    cluster_graph,
    edge_risk,
    overall_risk,
    partition_symbols,
)
from reposplit.agents.architect.prompts import ARCHITECT_SYSTEM, architect_user_prompt
from reposplit.core.base_agent import AgentError, BaseAgent
from reposplit.core.schemas import (
    AgentResult,
    ArchitectDecision,
    ClusterAssignment,
    DependencyCycle,
    DependencyGraph,
    DomainTopology,
    Keys,
    Phase,
    RiskLevel,
    SeveredEdge,
)

HIGH_RISK_CALL_WEIGHT = 5
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")


class ArchitectAgent(BaseAgent):
    name = "architect"
    phase = Phase.ARCHITECT
    requires = (Keys.REPO_MANIFEST,)
    produces = (Keys.DEPENDENCY_GRAPH, Keys.TOPOLOGY)
    uses_llm = True
    description = "Whole-repo AST parsing, NetworkX coupling metrics, Louvain community detection, Tarjan cycles."

    async def execute(self) -> AgentResult:
        graph = PythonRepoParser(self.ctx.repo_root).parse()
        self.bb.put(Keys.DEPENDENCY_GRAPH, graph)
        self.log(
            f"parsed {graph.file_count} modules, {len(graph.nodes)} symbols, {len(graph.edges)} edges",
            nodes=len(graph.nodes),
            edges=len(graph.edges),
        )
        if not any(n.kind in ("class", "function") for n in graph.nodes):
            raise AgentError("no classes or functions found to partition")

        topology = self._partition(graph)
        topology = self._resolve_helper_closures(topology, graph)
        topology = await self._refine_with_llm(topology, graph)
        topology = self._apply_topology_override(topology, graph)
        self._apply_mode(topology)
        self.bb.put(Keys.TOPOLOGY, topology)

        self.write_artifact("reports/dependency_graph.json", graph)
        self.write_artifact("reports/domain_topology.json", topology)

        services = topology.service_clusters()
        return self.ok(
            f"{len(services)} service cluster(s), {len(topology.severed_edges)} severed edge(s), "
            f"coupling {topology.initial_coupling:.2f} -> {topology.severed_coupling:.2f}, risk {topology.risk_level}",
            services=sorted(services),
            severed_edges=len(topology.severed_edges),
            modularity=topology.modularity,
            initial_coupling=topology.initial_coupling,
            severed_coupling=topology.severed_coupling,
            cycles=len(topology.cycles),
            human_override=topology.human_override,
        )

    # ---- deterministic partition & human overrides ---------------------------------

    def _partition(self, graph: DependencyGraph) -> DomainTopology:
        part = partition_symbols(graph, seed=self.config.seed)
        return build_domain_topology(graph, part.assignment, modularity_val=part.modularity)

    def _resolve_helper_closures(self, topology: DomainTopology, graph: DependencyGraph) -> DomainTopology:
        nodes = graph.node_map()
        assignment = {sid: topology.cluster_of(sid) or SHARED_KERNEL for sid in nodes}
        changed = False

        for sid, n in nodes.items():
            if n.kind != "function" or n.route or n.is_model or not n.name.startswith("_"):
                continue
            callers = [e.source for e in graph.edges if e.target == sid and e.kind == "call" and e.source in nodes]
            if not callers:
                continue
            caller_clusters = {assignment.get(c) for c in callers} - {SHARED_KERNEL, None}
            if len(caller_clusters) == 1:
                sole_cluster = next(iter(caller_clusters))
                if assignment.get(sid) != sole_cluster and sole_cluster in topology.clusters:
                    assignment[sid] = sole_cluster
                    changed = True
            elif len(caller_clusters) > 1:
                if assignment.get(sid) != SHARED_KERNEL:
                    assignment[sid] = SHARED_KERNEL
                    changed = True

        if changed:
            self.log("resolved helper closures into caller cluster(s)")
            return build_domain_topology(graph, assignment, modularity_val=topology.modularity)
        return topology

    def _apply_topology_override(self, topology: DomainTopology, graph: DependencyGraph) -> DomainTopology:
        overrides: dict[str, str] = dict(self.config.manual_assignments)
        if self.config.topology_override:
            p = Path(self.config.topology_override)
            if not p.exists():
                p = Path(self.ctx.repo_root) / self.config.topology_override
            if not p.exists():
                p = Path.cwd() / self.config.topology_override
            if p.exists():
                import json

                try:
                    with open(p, encoding="utf-8") as f:
                        data = json.load(f)
                        if isinstance(data, dict):
                            overrides.update(data)
                except Exception as ex:
                    self.log(f"failed to load topology override from {p}: {ex}", level="warning")

        if not overrides:
            return topology

        current_assignment: dict[str, str] = {}
        for sid in graph.node_map():
            current_assignment[sid] = topology.cluster_of(sid) or SHARED_KERNEL

        applied_count = 0
        for sid, node in graph.node_map().items():
            if sid in overrides:
                current_assignment[sid] = overrides[sid]
                applied_count += 1
            elif node.name in overrides:
                current_assignment[sid] = overrides[node.name]
                applied_count += 1
            elif node.module in overrides:
                current_assignment[sid] = overrides[node.module]
                applied_count += 1

        self.log(f"applied {applied_count} human topology override(s) across {len(overrides)} rule(s)")
        return build_domain_topology(
            graph,
            current_assignment,
            modularity_val=topology.modularity,
            human_override=True,
            rationale=f"Human-in-the-loop override applied ({len(overrides)} rule(s)).",
        )

    @staticmethod
    def _cluster_graph(severed: list[SeveredEdge]):
        return cluster_graph(severed)

    @staticmethod
    def _edge_risk(kind: str, weight: int) -> RiskLevel:
        return edge_risk(kind, weight)

    @staticmethod
    def _overall_risk(severed: list[SeveredEdge], cycles: list[DependencyCycle]) -> RiskLevel:
        return overall_risk(severed, cycles)

    # ---- LLM refinement ------------------------------------------------------------

    async def _refine_with_llm(self, topology: DomainTopology, graph: DependencyGraph) -> DomainTopology:
        default = ArchitectDecision(
            clusters=[
                ClusterAssignment(heuristic_name=n, proposed_name=n, rationale=c.rationale or "heuristic domain lexicon")
                for n, c in topology.clusters.items()
            ],
            high_risk_cut_points=[
                f"{s.source} -> {s.target}" for s in topology.severed_edges if s.risk in (RiskLevel.HIGH, RiskLevel.CRITICAL)
            ],
            risk_level=topology.risk_level,
            rationale=topology.rationale,
        )
        decision = await self.decide(
            system=ARCHITECT_SYSTEM,
            user=architect_user_prompt(self._summary(topology)),
            schema=ArchitectDecision,
            default=default,
        )
        renames: dict[str, str] = {}
        for assignment in decision.clusters:
            old, new = assignment.heuristic_name, assignment.proposed_name.strip()
            if old not in topology.clusters or old == SHARED_KERNEL:
                continue
            if new != old and _NAME_RE.match(new) and new not in topology.clusters and new not in renames.values():
                renames[old] = new
            topology.clusters[old].rationale = assignment.rationale or topology.clusters[old].rationale
        if renames:
            self.log(f"LLM renamed clusters: {renames}")
            topology.clusters = {renames.get(k, k): v for k, v in topology.clusters.items()}
            for c in topology.clusters.values():
                c.name = renames.get(c.name, c.name)
            for s in topology.severed_edges:
                s.source_cluster = renames.get(s.source_cluster, s.source_cluster)
                s.target_cluster = renames.get(s.target_cluster, s.target_cluster)
            topology.clusters = dict(sorted(topology.clusters.items()))
        topology.risk_level = decision.risk_level or topology.risk_level
        if decision.rationale:
            topology.rationale = decision.rationale
        return topology

    def _apply_mode(self, topology: DomainTopology) -> None:
        if self.config.mode != "strangler":
            return
        targets = set(self.config.target_services)
        if not targets:
            # Default strangler pick: the least-coupled, most stable service is the safest first extraction.
            candidates = [c for c in topology.clusters.values() if c.kind == "service"]
            best = min(candidates, key=lambda c: (c.metrics.ca + c.metrics.ce, c.name))
            targets = {best.name}
        unknown = targets - set(topology.clusters)
        if unknown:
            raise AgentError(f"strangler target(s) not found in topology: {sorted(unknown)}")
        for c in topology.clusters.values():
            if c.kind == "service":
                c.extract = c.name in targets
        self.log(f"strangler mode: extracting {sorted(targets)} only", level="warning")

    @staticmethod
    def _summary(topology: DomainTopology) -> str:
        lines: list[str] = []
        for name, c in topology.clusters.items():
            lines.append(
                f"- {name} [{c.kind}] Ca={c.metrics.ca} Ce={c.metrics.ce} I={c.metrics.instability} loc={c.loc}"
            )
            lines.append(f"    files: {', '.join(c.files)}")
            lines.append(f"    symbols: {', '.join(s.split('::')[-1] for s in c.symbols[:25])}")
        lines.append("severed edges:")
        for s in topology.severed_edges[:40]:
            lines.append(f"- {s.source} -> {s.target} ({s.kind} w={s.weight} risk={s.risk})")
        if topology.cycles:
            lines.append("cycles: " + "; ".join(" <-> ".join(c.members) for c in topology.cycles))
        return "\n".join(lines)
