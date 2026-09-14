"""AST parser + graph partition on the ShopMonolith benchmark."""

from __future__ import annotations

from pathlib import Path

from reposplit.agents.architect.ast_parser import PythonRepoParser
from reposplit.agents.architect.graph_metrics import (
    SHARED_KERNEL,
    coupling,
    cross_module_fraction,
    find_cycles,
    is_core_module,
    partition_symbols,
    symbol_graph,
)


def _edges(graph, kind: str) -> set[tuple[str, str]]:
    return {(e.source, e.target) for e in graph.edges if e.kind == kind}


def test_parser_resolves_cross_module_calls_and_fks(monolith_path: Path) -> None:
    g = PythonRepoParser(monolith_path).parse()
    calls = _edges(g, "call")
    assert ("routes/checkout.py::create_order", "routes/auth.py::find_user") in calls
    assert ("routes/checkout.py::create_order", "routes/catalog.py::reserve_stock") in calls
    assert ("routes/checkout.py::create_order", "services/payment.py::charge_payment") in calls
    fks = _edges(g, "fk")
    assert ("models.py::Order", "models.py::User") in fks
    assert ("models.py::OrderItem", "models.py::Product") in fks
    data = _edges(g, "data_access")
    assert ("routes/checkout.py::order_history", "models.py::Product") in data
    nodes = g.node_map()
    assert nodes["models.py::Order"].is_model and nodes["models.py::Order"].table == "orders"
    route = nodes["routes/checkout.py::create_order"].route
    assert route and route.path == "/api/v1/orders" and route.methods == ["POST"]
    assert nodes["routes/checkout.py::create_order"].body_keys == ["user_id", "items"]


def test_partition_separates_domains_and_routes_infra_to_shared_kernel(monolith_path: Path) -> None:
    g = PythonRepoParser(monolith_path).parse()
    part = partition_symbols(g, seed=42)
    assert is_core_module("app.py") and not is_core_module("routes/checkout.py")
    assert "app.py::create_app" in part.members[SHARED_KERNEL]
    owners = {sym: c for c, syms in part.members.items() for sym in syms}
    assert len({owners["models.py::User"], owners["models.py::Product"], owners["models.py::Order"]}) == 3
    # functions follow the data they touch
    assert owners["routes/checkout.py::create_order"] == owners["models.py::Order"]
    assert owners["routes/auth.py::find_user"] == owners["models.py::User"]
    assert owners["routes/catalog.py::reserve_stock"] == owners["models.py::Product"]
    assert 0 < part.modularity <= 1


def test_metrics_are_bounded(monolith_path: Path) -> None:
    g = PythonRepoParser(monolith_path).parse()
    sg = symbol_graph(g)
    part = partition_symbols(g)
    for name, syms in part.members.items():
        m = coupling(sg, set(syms))
        assert 0.0 <= m.instability <= 1.0, name
    assert 0.5 < cross_module_fraction(sg) <= 1.0  # a monolith is tangled by definition
    assert find_cycles(sg) == [] or all(len(c) > 1 for c in find_cycles(sg))


def test_partition_is_deterministic(monolith_path: Path) -> None:
    g = PythonRepoParser(monolith_path).parse()
    a = partition_symbols(g, seed=7).members
    b = partition_symbols(g, seed=7).members
    assert a == b
