from pathlib import Path

from reposplit.core.schemas import (
    Cluster,
    ContractPlan,
    DataPartitionPlan,
    DependencyGraph,
    DomainTopology,
    GraphNode,
    RouteInfo,
    ServiceContract,
)
from reposplit.generators.porting import FlaskPorter
from reposplit.generators.render import render


def test_db_template_renders_pool_pre_ping() -> None:
    content = render("service/db.py.j2", service="test_service")
    assert "pool_pre_ping" in content
    assert "create_engine" in content
    assert "session_scope" in content


def test_flask_porter_extracts_query_parameters(tmp_path: Path) -> None:
    code = """
from flask import Blueprint, jsonify, request

bp = Blueprint("items", __name__)

@bp.route("/items", methods=["GET"])
def list_items():
    page = request.args.get("page", 1, type=int)
    search = request.args.get("q", "")
    tag = request.args.get("tag")
    return jsonify({"items": [], "page": page, "search": search, "tag": tag})
"""
    mod_path = tmp_path / "routes.py"
    mod_path.write_text(code, encoding="utf-8")

    node = GraphNode(
        id="routes.py::list_items",
        kind="function",
        module="routes.py",
        name="list_items",
        lineno=7,
        end_lineno=13,
        route=RouteInfo(path="/items", methods=["GET"]),
    )
    graph = DependencyGraph(root=str(tmp_path), nodes=[node], edges=[])
    cluster = Cluster(name="item_service", symbols=[node.id])
    topo = DomainTopology(clusters={"item_service": cluster}, severed_edges=[])
    contracts = ContractPlan(services={"item_service": ServiceContract(service="item_service", port=8001, base_path="/items", endpoints=[])})
    data_plan = DataPartitionPlan(service_schemas={}, entities=[], severed_foreign_keys=[], sagas=[], projections=[])

    porter = FlaskPorter(tmp_path, graph, topo, contracts, data_plan, "item_service")
    ported = porter.port_function(node)

    assert ported.kind == "route"
    # Check signature has page, q, tag
    assert "page: int = 1" in ported.source
    assert 'q: str = ""' in ported.source
    assert "tag: str | None = None" in ported.source

    # Check body was rewritten to access them directly
    assert "request.args.get" not in ported.source
    assert "page = page" in ported.source
    assert "search = q" in ported.source
