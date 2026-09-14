"""Per-agent assertions on the shared offline pipeline run (see conftest.pipeline_run)."""

from __future__ import annotations

import ast
import py_compile
import re

import yaml

from reposplit.agents.governance.attestation import load_passport, verify_passport, verify_subjects
from reposplit.agents.parity.semantic_diff import diff, normalize
from reposplit.core.schemas import (
    ContractPlan,
    DataPartitionPlan,
    DomainTopology,
    GatewayPlan,
    Keys,
    ParityReport,
    Phase,
    ScaffoldManifest,
)


def test_run_completes_and_persists(pipeline_run) -> None:
    supervisor, summary = pipeline_run
    assert summary.status == Phase.COMPLETE, summary.error
    assert [r.agent for r in summary.results] == ["architect", "data", "contract", "strangler", "scaffold", "parity", "governance"]
    assert all(r.ok for r in summary.results)
    out = supervisor.output_dir
    assert (out / ".reposplit" / "blackboard.json").exists()
    assert (out / ".reposplit" / "events.jsonl").exists()
    assert len(summary.artifacts) > 50


def test_data_agent_severs_cross_service_fks_and_builds_saga(pipeline_run) -> None:
    supervisor, _ = pipeline_run
    plan = supervisor.bb.require(Keys.DATA_PLAN, DataPartitionPlan)
    severed = {(f.table, f.column) for f in plan.severed_foreign_keys}
    assert ("orders", "user_id") in severed and ("order_items", "product_id") in severed
    assert ("order_items", "order_id") not in severed  # same service: constraint kept
    saga = next(s for s in plan.sagas if s.name == "CreateOrderSaga")
    names = [st.name for st in saga.steps]
    assert names.index("ReserveStock") < names.index("ChargePayment") < names.index("FinalizeCreateOrder")
    by_name = {st.name: st for st in saga.steps}
    assert by_name["ReserveStock"].compensation == "release_stock" and by_name["ReserveStock"].compensation_exists
    assert by_name["ChargePayment"].compensation == "refund_payment" and by_name["ChargePayment"].compensation_exists
    proj = next(p for p in plan.projections if p.name == "OrderHistoryView")
    assert "products" in proj.source_tables and "users" in proj.source_tables
    assert plan.god_files == ["models.py"]


def test_isolated_schemas_are_valid_sqlalchemy(pipeline_run) -> None:
    supervisor, _ = pipeline_run
    topo = supervisor.bb.require(Keys.TOPOLOGY, DomainTopology)
    for svc in topo.service_clusters():
        path = supervisor.output_dir / "data" / "isolated_schemas" / svc / "models.py"
        src = path.read_text(encoding="utf-8")
        ast.parse(src)
        assert "db." not in src and "(Base)" in src
    order_models = (supervisor.output_dir / "data" / "isolated_schemas" / "order_service" / "models.py").read_text()
    assert re.search(r"user_id = Column\(Integer, nullable=False, index=True\)  # soft reference", order_models)


def test_contract_agent_emits_openapi_and_proto(pipeline_run) -> None:
    supervisor, _ = pipeline_run
    plan = supervisor.bb.require(Keys.CONTRACT_PLAN, ContractPlan)
    user = plan.services["user_service"]
    ops = {(e.operation_id, e.exposure, e.method) for e in user.endpoints}
    assert ("register", "public", "POST") in ops and ("find_user", "internal", "POST") in ops
    assert "/api/v1/users/{user_id}" in user.openapi["paths"]
    assert "service UserService" in user.proto and "rpc FindUser" in user.proto
    order = plan.services["order_service"]
    assert set(order.downstream) >= {"catalog_service", "payment_service", "user_service"}
    doc = yaml.safe_load((supervisor.output_dir / "contracts" / "order_service" / "openapi.yaml").read_text())
    assert doc["openapi"].startswith("3.0") and "/api/v1/orders" in doc["paths"]
    headers = {p["name"] for p in doc["paths"]["/api/v1/orders"]["post"]["parameters"]}
    assert {"X-User-Id", "X-Tenant-Id", "traceparent"} <= headers


def test_strangler_agent_weights_and_envoy_config(pipeline_run) -> None:
    supervisor, _ = pipeline_run
    plan = supervisor.bb.require(Keys.GATEWAY_PLAN, GatewayPlan)
    assert plan.routes and all(r.canary_weight + r.monolith_weight == 100 for r in plan.routes)
    envoy = yaml.safe_load((supervisor.output_dir / "gateway" / "envoy.yaml").read_text())
    clusters = {c["name"] for c in envoy["static_resources"]["clusters"]}
    assert "monolith" in clusters and "order_service" in clusters
    routes = envoy["static_resources"]["listeners"][0]["filter_chains"][0]["filters"][0]["typed_config"]["route_config"]["virtual_hosts"][0]["routes"]
    weighted = [r for r in routes if "weighted_clusters" in r.get("route", {})]
    assert weighted and weighted[0]["route"]["weighted_clusters"]["clusters"][0]["name"] == "monolith"
    assert (supervisor.output_dir / "gateway" / "canary_controller.py").exists()


def test_scaffold_generates_compilable_services(pipeline_run) -> None:
    supervisor, _ = pipeline_run
    manifest = supervisor.bb.require(Keys.SCAFFOLD_MANIFEST, ScaffoldManifest)
    assert set(manifest.services) == {"catalog_service", "order_service", "payment_service", "user_service"}
    for svc in manifest.services.values():
        for rel in svc.files:
            if rel.endswith(".py"):
                py_compile.compile(str(supervisor.output_dir / rel), doraise=True)
    main = (supervisor.output_dir / "services" / "order_service" / "app" / "main.py").read_text()
    assert "clients.user_service.find_user(" in main
    assert "clients.catalog_service.reserve_stock(" in main
    assert "raise HTTPException(status_code=404" in main
    assert "JSONResponse(content=order.to_dict(), status_code=201)" in main
    assert "TODO(auto-heal)" in main  # the cross-service JOIN is flagged, not hidden
    assert (supervisor.output_dir / "docker-compose.yml").exists()
    assert (supervisor.output_dir / "helm" / "reposplit" / "templates" / "deployment.yaml").exists()


def test_parity_suite_is_ordered_and_pending_offline(pipeline_run) -> None:
    supervisor, _ = pipeline_run
    report = supervisor.bb.require(Keys.PARITY_REPORT, ParityReport)
    assert report.mode == "suite_generated" and report.pending == report.total > 10
    ids = [c.id for c in report.cases]
    assert ids.index("001_register") < ids.index("002_login")
    assert next(i for i in ids if "create_order" in i) < next(i for i in ids if "cancel_order" in i)


def test_semantic_diff_masks_and_reports() -> None:
    a = {"id": 1, "token": "abc", "created_at": "2026-09-14T10:00:00Z", "items": [{"q": 1}], "total": 10.0}
    b = {"id": 1, "token": "xyz", "created_at": "2026-09-14T11:00:00Z", "items": [{"q": 1}], "total": 10}
    assert diff(normalize(a), normalize(b)) == []
    problems = diff(normalize({"a": 1, "b": [1, 2]}), normalize({"a": "1", "b": [1]}))
    assert any("type" in p for p in problems) and any("length" in p for p in problems)
    assert diff({"x": {"y": 1}}, {"x": {}}) == ["$.x.y: missing in service response"]


def test_governance_passport_verifies_and_detects_tampering(pipeline_run) -> None:
    supervisor, _ = pipeline_run
    path = supervisor.output_dir / "reports" / "migration_passport.json"
    passport = load_passport(path)
    ok, problems = verify_passport(passport)
    assert ok, problems
    assert verify_subjects(passport, supervisor.output_dir) == []
    assert passport.signer_did.startswith("did:key:z6Mk")
    pred = passport.statement.predicate
    assert pred["cutDecisionGraph"]["services"] == ["catalog_service", "order_service", "payment_service", "user_service"]
    assert pred["modelProvenance"]["prompts"] and pred["parityTestVerification"]["mode"] == "suite_generated"
    tampered = passport.model_copy(deep=True)
    tampered.statement.predicate["parityTestVerification"]["passRate"] = "100%"
    ok, problems = verify_passport(tampered)
    assert not ok and "payload does not match" in problems[0]
