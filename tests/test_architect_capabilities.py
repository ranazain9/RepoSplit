"""Tests for upgraded ArchitectAgent and AST parsing capabilities."""

from __future__ import annotations

import ast
from pathlib import Path

from reposplit.agents.architect.ast_parser import (
    PythonRepoParser,
    _classify_operation,
    _extract_returns,
)
from reposplit.agents.architect.graph_metrics import edge_risk
from reposplit.core.schemas import RiskLevel


def test_extract_returns_tuple_and_dict() -> None:
    src = """
def create_order():
    return {"order_id": 42, "status": "pending"}, 201
"""
    fn = ast.parse(src).body[0]
    fields, codes = _extract_returns(fn)
    assert "order_id" in fields
    assert "status" in fields
    assert codes == [201]


def test_extract_returns_jsonify_and_default_status() -> None:
    src = """
def get_profile():
    return jsonify(username="alice", email="alice@example.com")
"""
    fn = ast.parse(src).body[0]
    fields, codes = _extract_returns(fn)
    assert "username" in fields
    assert "email" in fields
    assert codes == [200]


def test_classify_operation() -> None:
    fn = ast.parse("def get_items(): pass").body[0]
    assert _classify_operation(fn, ["GET"]) == "READ"
    assert _classify_operation(fn, ["POST"]) == "MUTATING"
    assert _classify_operation(fn, ["DELETE"]) == "IDEMPOTENT"
    assert _classify_operation(fn, ["PUT"]) == "IDEMPOTENT"


def test_transaction_edge_detection(tmp_path: Path) -> None:
    code = """
from db import session

def helper_operation():
    return 1

def atomic_checkout():
    with session.begin():
        helper_operation()
"""
    f = tmp_path / "checkout.py"
    f.write_text(code, encoding="utf-8")

    parser = PythonRepoParser(tmp_path)
    graph = parser.parse()

    tx_edges = [e for e in graph.edges if e.kind == "transaction"]
    assert len(tx_edges) >= 1
    assert any("atomic_checkout" in e.source and "helper_operation" in e.target for e in tx_edges)
    assert edge_risk("transaction", 1) == RiskLevel.CRITICAL
