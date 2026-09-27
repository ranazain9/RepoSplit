"""Graph algorithms behind the Architect agent.

    Instability   I = Ce / (Ca + Ce)
    Modularity    Q = 1/2m * sum_ij [A_ij - k_i k_j / 2m] * delta(c_i, c_j)   (NetworkX Louvain)
    Cycles        Tarjan SCC (NetworkX strongly_connected_components)
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import PurePosixPath

import networkx as nx

from reposplit.core.schemas import (
    Cluster,
    CouplingMetrics,
    DependencyCycle,
    DependencyGraph,
    DomainTopology,
    GraphNode,
    RiskLevel,
    SeveredEdge,
)
from reposplit.utils.naming import score_domains, tokens

PARTITION_KINDS = {"class", "function"}
SYMBOL_EDGE_KINDS = {"call", "data_access", "fk", "inherits", "transaction"}
SHARED_KERNEL = "shared_kernel"


@dataclass
class Partition:
    """Symbol id -> cluster name, plus the cluster -> symbols reverse index."""

    assignment: dict[str, str]
    members: dict[str, list[str]]
    modularity: float


def symbol_graph(graph: DependencyGraph) -> nx.DiGraph:
    g = nx.DiGraph()
    for n in graph.nodes:
        if n.kind in PARTITION_KINDS:
            g.add_node(n.id, kind=n.kind, module=n.module, loc=n.loc, is_model=n.is_model)
    for e in graph.edges:
        if e.kind in SYMBOL_EDGE_KINDS and e.source in g and e.target in g:
            prev = g[e.source][e.target] if g.has_edge(e.source, e.target) else {"weight": 0, "data_weight": 0}
            w_mult = 5 if e.kind == "transaction" else 1
            g.add_edge(
                e.source,
                e.target,
                weight=prev["weight"] + e.weight * w_mult,
                data_weight=prev["data_weight"] + (e.weight if e.kind == "data_access" else 0),
            )
    return g


def is_core_module(module: str) -> bool:
    """Infrastructure files (app.py, db.py, config.py, seed.py ...) always belong to the shared kernel."""
    scores = score_domains(tokens(PurePosixPath(module).stem))
    return scores["core"] > 0 and all(v == 0 for k, v in scores.items() if k != "core")


def module_graph(graph: DependencyGraph) -> nx.DiGraph:
    g = nx.DiGraph()
    for n in graph.nodes:
        if n.kind == "module":
            g.add_node(n.id)
    for e in graph.edges:
        if e.kind == "import" and e.source in g and e.target in g and e.source != e.target:
            w = g[e.source][e.target]["weight"] if g.has_edge(e.source, e.target) else 0
            g.add_edge(e.source, e.target, weight=w + e.weight)
    return g


def detect_communities(g: nx.DiGraph, seed: int = 42, resolution: float = 1.0) -> list[set[str]]:
    und = nx.Graph()
    und.add_nodes_from(g.nodes)
    for u, v, data in g.edges(data=True):
        if u == v:
            continue
        w = und[u][v]["weight"] if und.has_edge(u, v) else 0
        und.add_edge(u, v, weight=w + data.get("weight", 1))
    if und.number_of_edges() == 0:
        return [{n} for n in und.nodes]
    comms = nx.community.louvain_communities(und, weight="weight", seed=seed, resolution=resolution)
    return [set(c) for c in comms]


def modularity(g: nx.DiGraph, communities: list[set[str]]) -> float:
    und = g.to_undirected()
    if und.number_of_edges() == 0 or not communities:
        return 0.0
    return float(nx.community.modularity(und, communities, weight="weight"))


def find_cycles(g: nx.DiGraph) -> list[list[str]]:
    return [sorted(scc) for scc in nx.strongly_connected_components(g) if len(scc) > 1]


def coupling(g: nx.DiGraph, members: set[str]) -> CouplingMetrics:
    ca = sum(d["weight"] for u, v, d in g.in_edges(members, data=True) if u not in members)
    ce = sum(d["weight"] for u, v, d in g.out_edges(members, data=True) if v not in members)
    inst = ce / (ca + ce) if (ca + ce) else 0.0
    return CouplingMetrics(ca=int(ca), ce=int(ce), instability=round(inst, 3))


def cross_module_fraction(g: nx.DiGraph) -> float:
    total = sum(d["weight"] for _, _, d in g.edges(data=True))
    if not total:
        return 0.0
    cross = sum(d["weight"] for u, v, d in g.edges(data=True) if g.nodes[u]["module"] != g.nodes[v]["module"])
    return round(cross / total, 3)


def cross_cluster_fraction(g: nx.DiGraph, assignment: dict[str, str], service_clusters: set[str]) -> float:
    total = sum(d["weight"] for _, _, d in g.edges(data=True))
    if not total:
        return 0.0
    cross = 0
    for u, v, d in g.edges(data=True):
        cu, cv = assignment.get(u), assignment.get(v)
        if cu != cv and cu in service_clusters and cv in service_clusters:
            cross += d["weight"]
    return round(cross / total, 3)


# ---- naming & post-processing ------------------------------------------------------------


def _dominant_domain(symbols: list[str], nodes: dict[str, GraphNode]) -> tuple[str, str]:
    """Return (domain, fallback_stem). Domain may be 'core' or 'unknown'."""
    words: list[str] = []
    stems: Counter[str] = Counter()
    for sid in symbols:
        n = nodes[sid]
        words += tokens(n.name)
        stem = PurePosixPath(n.module).stem
        stems[stem] += 1
        words += tokens(stem)
        if n.table:
            words += tokens(n.table)
    scores = score_domains(words)
    best = max(scores.items(), key=lambda kv: (kv[1], kv[0] != "core"))
    fallback = stems.most_common(1)[0][0] if stems else "misc"
    if best[1] == 0:
        return "unknown", fallback
    return best[0], fallback


def _refine_by_data_affinity(g: nx.DiGraph, labels: dict[str, int], passes: int = 3) -> dict[str, int]:
    """Functions follow the data they touch (Mono2Micro-style data-ownership refinement).

    A function with data_access edges moves to the community owning most of the models it reads or
    writes; a function with no data access follows its strongest call neighbours. Models anchor.
    """
    labels = dict(labels)
    for _ in range(passes):
        changed = False
        for sid in sorted(labels):
            if g.nodes[sid].get("is_model"):
                continue
            data: Counter[int] = Counter()
            calls: Counter[int] = Counter()
            for _, v, d in g.out_edges(sid, data=True):
                if v not in labels:
                    continue
                if g.nodes[v].get("is_model"):
                    data[labels[v]] += d["data_weight"] or d["weight"]
                else:
                    calls[labels[v]] += d["weight"]
            for u, _, d in g.in_edges(sid, data=True):
                if u in labels and not g.nodes[u].get("is_model"):
                    calls[labels[u]] += d["weight"]
            pool = data or calls
            if not pool:
                continue
            current = labels[sid]
            best_w = max(pool.values())
            winners = sorted(c for c, w in pool.items() if w == best_w)
            target = current if current in winners else winners[0]
            if target != current:
                labels[sid] = target
                changed = True
        if not changed:
            break
    return labels


def partition_symbols(
    graph: DependencyGraph, seed: int = 42, resolution: float = 1.0, min_cluster_size: int = 2
) -> Partition:
    nodes = graph.node_map()
    g = symbol_graph(graph)

    # 0. Infrastructure symbols are shared kernel by construction and never influence Louvain.
    kernel_symbols = {sid for sid in g.nodes if is_core_module(g.nodes[sid]["module"])}
    work = g.subgraph([sid for sid in g.nodes if sid not in kernel_symbols]).copy()

    communities = detect_communities(work, seed=seed, resolution=resolution)
    labels = {sid: idx for idx, comm in enumerate(communities) for sid in comm}
    labels = _refine_by_data_affinity(work, labels)
    refined = [set(s for s, lab in labels.items() if lab == idx) for idx in range(len(communities))]
    refined = [c for c in refined if c]
    q = modularity(work, refined)

    # 1. Name communities by dominant domain; merge same-domain communities; route 'core' to shared kernel.
    named: dict[str, set[str]] = defaultdict(set)
    named[SHARED_KERNEL] |= kernel_symbols
    for comm in refined:
        domain, stem = _dominant_domain(sorted(comm), nodes)
        if domain == "core":
            named[SHARED_KERNEL] |= comm
        elif domain == "unknown":
            named[f"{stem}_service"] |= comm
        else:
            named[f"{domain}_service"] |= comm
    if not named[SHARED_KERNEL]:
        del named[SHARED_KERNEL]

    # 2. Absorb tiny communities into the neighbour they are most coupled to (else shared kernel).
    assignment = {sid: name for name, members in named.items() for sid in members}
    for name in list(named):
        if name == SHARED_KERNEL or len(named[name]) >= min_cluster_size:
            continue
        members = named.pop(name)
        pull: Counter[str] = Counter()
        for sid in members:
            for _, v, d in g.out_edges(sid, data=True):
                if assignment.get(v) not in (name, None):
                    pull[assignment[v]] += d["weight"]
            for u, _, d in g.in_edges(sid, data=True):
                if assignment.get(u) not in (name, None):
                    pull[assignment[u]] += d["weight"]
        target = pull.most_common(1)[0][0] if pull else SHARED_KERNEL
        named[target] |= members
        for sid in members:
            assignment[sid] = target

    members = {name: sorted(m) for name, m in sorted(named.items())}
    return Partition(assignment=assignment, members=members, modularity=round(q, 3))


HIGH_RISK_CALL_WEIGHT = 5


def edge_risk(kind: str, weight: int, in_loop: bool = False) -> RiskLevel:
    if in_loop:
        return RiskLevel.CRITICAL
    if kind == "transaction":
        return RiskLevel.CRITICAL
    if kind in ("fk", "inherits", "data_access"):
        return RiskLevel.HIGH
    if weight > HIGH_RISK_CALL_WEIGHT:
        return RiskLevel.HIGH
    if weight > 1:
        return RiskLevel.MEDIUM
    return RiskLevel.LOW


def overall_risk(severed: list[SeveredEdge], cycles: list[DependencyCycle]) -> RiskLevel:
    criticals = sum(1 for s in severed if s.risk == RiskLevel.CRITICAL)
    highs = sum(1 for s in severed if s.risk == RiskLevel.HIGH)
    service_cycle = any(all(not m.endswith(".py") for m in c.members) for c in cycles)
    if criticals > 5 or highs > 10:
        return RiskLevel.CRITICAL
    if criticals > 0 or service_cycle or highs > 5:
        return RiskLevel.HIGH
    if highs > 0 or len(severed) > 5:
        return RiskLevel.MEDIUM
    return RiskLevel.LOW


def cluster_graph(severed: list[SeveredEdge]) -> nx.DiGraph:
    g = nx.DiGraph()
    for s in severed:
        g.add_edge(s.source_cluster, s.target_cluster)
    return g


def build_domain_topology(
    graph: DependencyGraph,
    assignment: dict[str, str],
    modularity_val: float = 0.0,
    human_override: bool = False,
    rationale: str = "",
) -> DomainTopology:
    nodes = graph.node_map()
    g = symbol_graph(graph)

    members: dict[str, list[str]] = defaultdict(list)
    for sid, cname in assignment.items():
        members[cname].append(sid)

    clusters: dict[str, Cluster] = {}
    files_by_cluster: dict[str, set[str]] = defaultdict(set)
    for cname, symbols in members.items():
        files = sorted({nodes[s].module for s in symbols if s in nodes})
        files_by_cluster[cname] = set(files)
        clusters[cname] = Cluster(
            name=cname,
            kind="shared_kernel" if cname == SHARED_KERNEL else "service",
            files=files,
            symbols=sorted(symbols),
            metrics=coupling(g, set(symbols)),
            loc=sum(nodes[s].loc for s in symbols if s in nodes),
            extract=cname != SHARED_KERNEL,
        )

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
        cu, cv = assignment.get(e.source), assignment.get(e.target)
        if not cu or not cv or cu == cv or cu not in service_names or cv not in service_names:
            continue
        warning = None
        if e.in_loop:
            warning = (
                "Chatty boundary: target is invoked inside a loop across service boundaries (N+1 remote calls). "
                "Recommend bulk API batching or co-locating."
            )
        severed.append(
            SeveredEdge(
                source=e.source,
                target=e.target,
                source_cluster=cu,
                target_cluster=cv,
                kind=e.kind,
                weight=e.weight,
                risk=edge_risk(e.kind, e.weight, in_loop=e.in_loop),
                in_loop=e.in_loop,
                chatty_warning=warning,
            )
        )
    severed.sort(key=lambda s: (-s.weight, s.source))

    split_files = sorted(
        f for f in assigned_files if sum(1 for fs in files_by_cluster.values() if f in fs) > 1
    )

    cycles: list[DependencyCycle] = []
    cg = cluster_graph(severed)
    for scc in find_cycles(cg):
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

    risk = overall_risk(severed, cycles)
    return DomainTopology(
        clusters=dict(sorted(clusters.items())),
        severed_edges=severed,
        cycles=cycles,
        split_files=split_files,
        modularity=modularity_val,
        initial_coupling=cross_module_fraction(g),
        severed_coupling=cross_cluster_fraction(g, assignment, service_names),
        risk_level=risk,
        human_override=human_override,
        rationale=rationale
        or (
            "Human-in-the-loop architectural override applied over symbol assignments."
            if human_override
            else "Louvain community detection over the symbol-level call/data graph; "
            "infrastructure symbols routed to the shared kernel; tiny communities absorbed by strongest neighbour."
        ),
    )
