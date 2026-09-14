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

from reposplit.core.schemas import CouplingMetrics, DependencyGraph, GraphNode
from reposplit.utils.naming import score_domains, tokens

PARTITION_KINDS = {"class", "function"}
SYMBOL_EDGE_KINDS = {"call", "data_access", "fk", "inherits"}
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
            g.add_edge(
                e.source,
                e.target,
                weight=prev["weight"] + e.weight,
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
