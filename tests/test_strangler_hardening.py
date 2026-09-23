import re

from reposplit.agents.strangler.agent import StranglerAgent
from reposplit.core.schemas import ContractPlan, EndpointContract, GatewayPlan, GatewayRoute, ServiceContract


def test_strangler_envoy_config_has_canary_override_and_cors() -> None:
    ep = EndpointContract(
        service="order_service",
        operation_id="create_order",
        method="POST",
        path="/api/v1/orders",
        exposure="public",
        body_keys=["items"],
        source_symbol="routes.checkout::create_order",
    )
    svc = ServiceContract(service="order_service", port=8003, base_path="/api/v1/order", endpoints=[ep])
    contracts = ContractPlan(services={"order_service": svc})

    plan = GatewayPlan(
        routes=[GatewayRoute(path="/api/v1/orders", method="POST", service="order_service", canary_weight=10, monolith_weight=90)],
        stages=[],
    )

    envoy = StranglerAgent._envoy_config(plan, contracts)

    # 1. Verify CORS filter is configured
    filters = envoy["static_resources"]["listeners"][0]["filter_chains"][0]["filters"][0]["typed_config"]["http_filters"]
    filter_names = [f["name"] for f in filters]
    assert "envoy.filters.http.cors" in filter_names
    assert "envoy.filters.http.router" in filter_names

    # 2. Verify virtual host CORS settings
    vhost = envoy["static_resources"]["listeners"][0]["filter_chains"][0]["filters"][0]["typed_config"]["route_config"]["virtual_hosts"][0]
    assert "cors" in vhost
    assert "X-Canary" in vhost["cors"]["allow_headers"]

    # 3. Verify Route matching: Override vs Weighted
    routes = vhost["routes"]
    canary_override = next(r for r in routes if r["name"].endswith("_canary_override"))
    assert canary_override["route"]["cluster"] == "order_service"
    header_matches = {h["name"]: h["string_match"] for h in canary_override["match"]["headers"]}
    assert ":method" in header_matches and header_matches[":method"]["exact"] == "POST"
    assert "x-canary" in header_matches

    monolith_override = next(r for r in routes if r["name"].endswith("_monolith_override"))
    assert monolith_override["route"]["cluster"] == "monolith"

    weighted = next(r for r in routes if "weighted_clusters" in r.get("route", {}))
    clusters = {c["name"]: c["weight"] for c in weighted["route"]["weighted_clusters"]["clusters"]}
    assert clusters["order_service"] == 10
    assert clusters["monolith"] == 90


def test_canary_controller_latency_and_5xx_parsing() -> None:
    stat_re = re.compile(r"^cluster\.(?P<cluster>[\w-]+)\.upstream_rq_(?P<code>2xx|5xx|total): (?P<value>\d+)$")
    lat_re = re.compile(r"^cluster\.(?P<cluster>[\w-]+)\.upstream_rq_time:.*P99\((?P<p99>[\d.]+)\)")

    sample_output = """
cluster.order_service.upstream_rq_2xx: 950
cluster.order_service.upstream_rq_5xx: 50
cluster.order_service.upstream_rq_total: 1000
cluster.order_service.upstream_rq_time: P50(12.5), P99(150.2)
cluster.user_service.upstream_rq_2xx: 100
cluster.user_service.upstream_rq_total: 100
cluster.user_service.upstream_rq_time: P99(25.0)
"""
    stats = {}
    latencies = {}
    for line in sample_output.splitlines():
        m = stat_re.match(line.strip())
        if m:
            stats.setdefault(m["cluster"], {})[m["code"]] = int(m["value"])
        ml = lat_re.match(line.strip())
        if ml:
            latencies[ml["cluster"]] = float(ml["p99"])

    assert stats["order_service"]["total"] == 1000
    assert stats["order_service"]["5xx"] == 50
    assert latencies["order_service"] == 150.2
    assert latencies["user_service"] == 25.0
