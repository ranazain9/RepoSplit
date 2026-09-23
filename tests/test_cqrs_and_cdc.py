"""Tests for CQRS Outbox, CDC Generator, and Multi-Provider Cascade."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import BaseModel

from reposplit.core.schemas import (
    Cluster,
    DataPartitionPlan,
    DomainTopology,
    EntityModel,
)
from reposplit.generators.cdc import CDCGenerator
from reposplit.llm.mock_provider import MockProvider
from reposplit.llm.provider import CascadeProvider, LLMError, LLMProvider, build_provider


class DummyFailingProvider(LLMProvider):
    name = "failing"
    model = "fail-1"

    async def complete_structured(self, **kwargs):
        raise LLMError("simulated rate limit 429")

    async def complete_text(self, **kwargs):
        raise LLMError("simulated rate limit 429")


class SampleDecision(BaseModel):
    choice: str


@pytest.mark.asyncio
async def test_cascade_provider_failover() -> None:
    failing = DummyFailingProvider()
    mock = MockProvider()
    cascade = CascadeProvider([failing, mock])

    # Failing provider should be skipped and mock should provide default
    res = await cascade.complete_structured(
        system="test",
        user="test",
        schema=SampleDecision,
        default=SampleDecision(choice="fallback_ok"),
    )
    assert res.choice == "fallback_ok"


def test_build_provider_cascade(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    provider = build_provider("cascade")
    assert isinstance(provider, CascadeProvider)
    assert len(provider.providers) >= 2  # Groq + Mock


def test_cdc_generator_emits_valid_artifacts(tmp_path: Path) -> None:
    entities = [
        EntityModel(
            class_name="Order",
            table="orders",
            module="models.py",
            symbol="models.py::Order",
            columns=[],
        ),
        EntityModel(
            class_name="User",
            table="users",
            module="models.py",
            symbol="models.py::User",
            columns=[],
        ),
    ]
    data_plan = DataPartitionPlan(
        entities=entities,
        service_schemas={"order_service": ["orders"], "user_service": ["users"]},
        severed_foreign_keys=[],
        sagas=[],
        projections=[],
    )
    topology = DomainTopology(
        clusters={
            "order_service": Cluster(name="order_service", kind="service"),
            "user_service": Cluster(name="user_service", kind="service"),
        },
        severed_edges=[],
    )

    gen = CDCGenerator(tmp_path, data_plan, topology)
    produced = gen.generate()

    assert "data/cdc/debezium_connector.json" in produced
    assert "data/cdc/sync_daemon.py" in produced

    deb_path = tmp_path / "data" / "cdc" / "debezium_connector.json"
    assert deb_path.exists()
    deb_data = json.loads(deb_path.read_text(encoding="utf-8"))
    assert "public.orders" in deb_data["config"]["table.include.list"]
    assert "public.users" in deb_data["config"]["table.include.list"]

    daemon_path = tmp_path / "data" / "cdc" / "sync_daemon.py"
    assert daemon_path.exists()
    assert "sync_tables" in daemon_path.read_text(encoding="utf-8")
