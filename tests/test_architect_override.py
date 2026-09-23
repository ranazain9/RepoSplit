"""Test Human-in-the-Loop topology overrides for the Architect Agent."""

from __future__ import annotations

import json
from pathlib import Path

from reposplit.agents.architect.agent import ArchitectAgent
from reposplit.agents.architect.ast_parser import PythonRepoParser
from reposplit.agents.architect.graph_metrics import (
    build_domain_topology,
    partition_symbols,
)
from reposplit.core.base_agent import AgentContext
from reposplit.core.blackboard import Blackboard
from reposplit.core.schemas import Keys, RunConfig
from reposplit.llm.provider import build_provider


def test_build_domain_topology_deterministic_and_override(monolith_path: Path) -> None:
    g = PythonRepoParser(monolith_path).parse()
    part = partition_symbols(g, seed=42)

    # 1. Baseline topology
    base_topo = build_domain_topology(g, part.assignment, modularity_val=part.modularity)
    assert not base_topo.human_override
    assert base_topo.severed_coupling < base_topo.initial_coupling

    # 2. Human override: move User model to a custom cluster
    new_assignment = dict(part.assignment)
    user_symbol = "models.py::User"
    assert user_symbol in new_assignment
    original_cluster = new_assignment[user_symbol]
    new_assignment[user_symbol] = "custom_auth_service"

    override_topo = build_domain_topology(
        g,
        new_assignment,
        modularity_val=part.modularity,
        human_override=True,
    )
    assert override_topo.human_override
    assert "custom_auth_service" in override_topo.clusters
    assert user_symbol in override_topo.clusters["custom_auth_service"].symbols
    assert user_symbol not in override_topo.clusters[original_cluster].symbols


async def test_architect_agent_applies_file_override(monolith_path: Path, tmp_path: Path) -> None:
    override_file = tmp_path / "test_override.json"
    override_file.write_text(
        json.dumps({"models.py::User": "security_service"}),
        encoding="utf-8",
    )

    config = RunConfig(
        repo_path=str(monolith_path),
        output_dir=str(tmp_path),
        topology_override=str(override_file),
    )
    bb = Blackboard("test_run", tmp_path)
    ctx = AgentContext(
        config=config,
        llm=build_provider("mock"),
        blackboard=bb,
        output_dir=tmp_path,
        repo_root=monolith_path,
    )
    agent = ArchitectAgent(ctx)

    res = await agent.execute()
    assert res.ok
    assert res.metrics.get("human_override") is True

    topo = agent.bb.get(Keys.TOPOLOGY)
    assert topo.human_override
    assert "security_service" in topo.clusters
    assert "models.py::User" in topo.clusters["security_service"].symbols
