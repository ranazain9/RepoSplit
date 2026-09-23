import ast
from pathlib import Path

from reposplit.agents.data.agent import SQLA_IMPORT, DataAgent
from reposplit.agents.data.schema_parser import parse_entities, sever_column_source
from reposplit.core.base_agent import AgentContext
from reposplit.core.blackboard import Blackboard
from reposplit.core.schemas import DependencyGraph, GraphNode, RiskLevel, RunConfig
from reposplit.llm.mock_provider import MockProvider


def test_schema_parser_extracts_enterprise_types_and_ondelete(tmp_path: Path) -> None:
    code = """
import enum
from sqlalchemy import Column, Integer, String, BigInteger, Numeric, JSON, Date, Enum, ForeignKey
from db import db

class UserStatus(str, enum.Enum):
    ACTIVE = "active"
    BANNED = "banned"

class EnterpriseEntity(db.Model):
    __tablename__ = "enterprise_records"

    id = Column(BigInteger, primary_key=True)
    metadata_json = Column(JSON, nullable=True)
    balance = Column(Numeric(12, 2), nullable=False)
    status = Column(Enum(UserStatus), nullable=False)
    tenant_ref = Column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
"""
    mod_file = tmp_path / "models.py"
    mod_file.write_text(code, encoding="utf-8")

    node = GraphNode(
        id="models.EnterpriseEntity",
        kind="class",
        module="models.py",
        name="EnterpriseEntity",
        is_model=True,
        table="enterprise_records",
        lineno=10,
        end_lineno=19,
    )
    graph = DependencyGraph(root=str(tmp_path), nodes=[node], edges=[])
    entities = parse_entities(tmp_path, graph)

    assert len(entities) == 1
    ent = entities[0]
    assert ent.table == "enterprise_records"
    col_map = {c.name: c for c in ent.columns}

    assert "id" in col_map and col_map["id"].type_expr == "BigInteger"
    assert "metadata_json" in col_map and col_map["metadata_json"].type_expr == "JSON"
    assert "balance" in col_map and "Numeric" in col_map["balance"].type_expr
    assert "status" in col_map and "Enum" in col_map["status"].type_expr
    assert "tenant_ref" in col_map
    assert col_map["tenant_ref"].foreign_key == "tenants.id"
    assert col_map["tenant_ref"].ondelete == "CASCADE"


def test_data_agent_sever_foreign_keys_cascade_detection(tmp_path: Path) -> None:
    code = """
from sqlalchemy import Column, Integer, ForeignKey
from db import db

class Invoice(db.Model):
    __tablename__ = "invoices"

    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False)
    profile_id = Column(Integer, ForeignKey("profiles.id"), nullable=True)
"""
    mod_file = tmp_path / "invoice_models.py"
    mod_file.write_text(code, encoding="utf-8")

    node = GraphNode(
        id="invoice_models.Invoice",
        kind="class",
        module="invoice_models.py",
        name="Invoice",
        is_model=True,
        table="invoices",
        lineno=5,
        end_lineno=11,
    )
    graph = DependencyGraph(root=str(tmp_path), nodes=[node], edges=[])
    entities = parse_entities(tmp_path, graph)

    cfg = RunConfig(repo_path=str(tmp_path), output_dir=str(tmp_path / "out"), provider="mock")
    bb = Blackboard("run_test", tmp_path / "out")
    ctx = AgentContext(config=cfg, llm=MockProvider(), blackboard=bb, output_dir=tmp_path / "out", repo_root=tmp_path)
    agent = DataAgent(ctx)

    owner_of_table = {
        "invoices": "billing_service",
        "accounts": "account_service",
        "profiles": "user_service",
    }

    severed = agent._sever_foreign_keys(entities, owner_of_table)
    assert len(severed) == 2

    cascade_fk = next(f for f in severed if f.column == "account_id")
    assert cascade_fk.cascade_delete is True
    assert cascade_fk.risk == RiskLevel.CRITICAL
    assert "CASCADE DELETED REMOVED" in cascade_fk.after
    assert "WARNING: On-delete CASCADE severed across boundary" in cascade_fk.migration_sql

    regular_fk = next(f for f in severed if f.column == "profile_id")
    assert regular_fk.cascade_delete is False
    assert regular_fk.risk == RiskLevel.MEDIUM
    assert "CASCADE DELETED REMOVED" not in regular_fk.after


def test_sqla_import_and_sever_column_source() -> None:
    assert "JSON" in SQLA_IMPORT
    assert "BigInteger" in SQLA_IMPORT
    assert "Numeric" in SQLA_IMPORT
    assert "Enum" in SQLA_IMPORT
    assert "SmallInteger" in SQLA_IMPORT

    # Test AST parsing of SQLA_IMPORT
    ast.parse(SQLA_IMPORT)

    # Test sever_column_source with ondelete
    raw_col = 'tenant_id = Column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)'
    severed = sever_column_source(raw_col, uuid_refs=False)
    assert "ForeignKey" not in severed
    assert "ondelete" not in severed
    assert "index=True" in severed
