"""Architect Agent - Phase 1: whole-system AST comprehension and domain partitioning."""

from __future__ import annotations

import re
from collections import defaultdict

from reposplit.agents.architect.ast_parser import PythonRepoParser
from reposplit.agents.architect.graph_metrics import (
    SHARED_KERNEL,
    coupling,
    cross_cluster_fraction,
    cross_module_fraction,
    find_cycles,
    module_graph,
    partition_symbols,
    symbol_graph,
)
from reposplit.agents.architect.prompts import ARCHITECT_SYSTEM, architect_user_prompt
from reposplit.core.base_agent import AgentError, BaseAgent
from reposplit.core.schemas import (
    AgentResult,
    ArchitectDecision,
    Cluster,
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
        topology = await self._refine_with_llm(topology, graph)
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
        )

    # ---- deterministic partition ---------------------------------------------------

    def _partition(self, graph: DependencyGraph) -> DomainTopology:
        nodes = graph.node_map()
        g = symbol_graph(graph)
        part = partition_symbols(graph, seed=self.config.seed)

        clusters: dict[str, Cluster] = {}
        files_by_cluster: dict[str, set[str]] = defaultdict(set)
        for cname, symbols in part.members.items():
            files = sorted({nodes[s].module for s in symbols})
            files_by_cluster[cname] = set(files)
            clusters[cname] = Cluster(
                name=cname,
                kind="shared_kernel" if cname == SHARED_KERNEL else "service",
                files=files,
                symbols=symbols,
                metrics=coupling(g, set(symbols)),
                loc=sum(nodes[s].loc for s in symbols),
                extract=cname != SHARED_KERNEL,
            )
        # Files with no partitionable symbols (db.py, config.py, ...) belong to the shared kernel.
        assigned_files = {f for fs in files_by_cluster.values() for f in fs}
        orphan_files = sorted(n.module for n in graph.nodes if n.kind == "module" and n.module not in assigned_files)
        if orphan_files:
            kernel = clusters.get(SHARED_KERNEL) or Cluster(
                name=SHARED_KERNEL, kind="shared_kernel", files=[], symbols=[], metrics=coupling(g, set()), extract=False
            )
            kernel.files = sorted(set(kernel.files) | set(orphan_files))
            clusters[SHARED_KERNEL] = kernel

        service_names = {c for c, cl in clusters.items() if cl.kind == "service"}
        severed: list[SeveredEdge] = []
        for e in graph.edges:
            cu, cv = part.assignment.get(e.source), part.assignment.get(e.target)
            if not cu or not cv or cu == cv or cu not in service_names or cv not in service_names:
                continue
            severed.append(
                SeveredEdge(
                    source=e.source,
                    target=e.target,
                    source_cluster=cu,
                    target_cluster=cv,
                    kind=e.kind,
                    weight=e.weight,
                    risk=self._edge_risk(e.kind, e.weight),
                )
            )
        severed.sort(key=lambda s: (-s.weight, s.source))

        split_files = sorted(
            f for f in assigned_files if sum(1 for fs in files_by_cluster.values() if f in fs) > 1
        )

        cycles: list[DependencyCycle] = []
        cluster_g = self._cluster_graph(severed)
        for scc in find_cycles(cluster_g):
            cycles.append(
                DependencyCycle(
                    members=scc,
                    resolution="Cyclic service dependency: introduce an event-driven intermediary (outbox event) "
                    "or a shared DTO contract so one direction becomes asynchronous.",
                )
            )
        for scc in find_cycles(module_graph(graph)):
            cycles.append(
                DependencyCycle(
                    members=scc,
                    resolution="Circular module import: hoist shared symbols into the shared kernel or use late imports.",
                )
            )

        risk = self._overall_risk(severed, cycles)
        return DomainTopology(
            clusters=dict(sorted(clusters.items())),
            severed_edges=severed,
            cycles=cycles,
            split_files=split_files,
            modularity=part.modularity,
            initial_coupling=cross_module_fraction(g),
            severed_coupling=cross_cluster_fraction(g, part.assignment, service_names),
            risk_level=risk,
            rationale="Louvain community detection over the symbol-level call/data graph; "
            "infrastructure symbols routed to the shared kernel; tiny communities absorbed by strongest neighbour.",
        )

    @staticmethod
    def _cluster_graph(severed: list[SeveredEdge]):
        import networkx as nx

        g = nx.DiGraph()
        for s in severed:
            g.add_edge(s.source_cluster, s.target_cluster)
        return g

    @staticmethod
    def _edge_risk(kind: str, weight: int) -> RiskLevel:
        if kind in ("fk", "inherits"):
            return RiskLevel.HIGH
        if kind == "data_access":
            return RiskLevel.HIGH
        if weight > HIGH_RISK_CALL_WEIGHT:
            return RiskLevel.HIGH
        if weight > 1:
            return RiskLevel.MEDIUM
        return RiskLevel.LOW

    @staticmethod
    def _overall_risk(severed: list[SeveredEdge], cycles: list[DependencyCycle]) -> RiskLevel:
        highs = sum(1 for s in severed if s.risk == RiskLevel.HIGH)
        service_cycle = any(all(not m.endswith(".py") for m in c.members) for c in cycles)
        if highs > 10:
            return RiskLevel.CRITICAL
        if service_cycle or highs > 5:
            return RiskLevel.HIGH
        if highs > 0 or len(severed) > 5:
            return RiskLevel.MEDIUM
        return RiskLevel.LOW

    # ---- LLM refinement ------------------------------------------------------------

    async def _refine_with_llm(self, topology: DomainTopology, graph: DependencyGraph) -> DomainTopology:
        default = ArchitectDecision(
            clusters=[
                ClusterAssignment(heuristic_name=n, proposed_name=n, rationale=c.rationale or "heuristic domain lexicon")
                for n, c in topology.clusters.items()
            ],
            high_risk_cut_points=[f"{s.source} -> {s.target}" for s in topology.severed_edges if s.risk == RiskLevel.HIGH],
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
