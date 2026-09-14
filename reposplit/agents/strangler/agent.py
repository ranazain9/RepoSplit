"""Strangler Fig & Gateway Agent - Phase 4: progressive, reversible migration.

Generates an Envoy front proxy that keeps the monolith as the default upstream and shifts a
weighted slice of each extracted route to its new service. Outlier detection ejects a failing
canary automatically; `canary_controller.py` implements the error-rate rollback policy.
"""

from __future__ import annotations

from typing import Any

import yaml

from reposplit.core.base_agent import BaseAgent
from reposplit.core.schemas import (
    AgentResult,
    CanaryStage,
    ContractPlan,
    GatewayPlan,
    GatewayRoute,
    Keys,
    Phase,
)
from reposplit.generators.render import render

STAGES: list[tuple[int, int, str]] = [
    (10, 30, "5xx rate < 0.1% and p99 latency within 1.2x of monolith"),
    (25, 60, "5xx rate < 0.1%, parity suite green on sampled traffic"),
    (50, 120, "5xx rate < 0.1%, no saga compensation storms (< 1%/min)"),
    (100, 0, "monolith route can be decommissioned; keep 0% weight for 7 days as rollback path"),
]


class StranglerAgent(BaseAgent):
    name = "strangler"
    phase = Phase.STRANGLER
    requires = (Keys.CONTRACT_PLAN,)
    produces = (Keys.GATEWAY_PLAN,)
    uses_llm = False
    description = "Envoy/Kong canary routing, staged migration plan, automatic error-rate rollback."

    async def execute(self) -> AgentResult:
        contracts = self.bb.require(Keys.CONTRACT_PLAN, ContractPlan)
        weight = self.config.canary_weight
        routes: list[GatewayRoute] = []
        for svc, contract in contracts.services.items():
            for ep in contract.endpoints:
                if ep.exposure != "public":
                    continue
                routes.append(
                    GatewayRoute(path=ep.path, method=ep.method, service=svc, canary_weight=weight, monolith_weight=100 - weight)
                )
        routes.sort(key=lambda r: (r.path, r.method))
        stages = [
            CanaryStage(stage=i + 1, canary_weight=w, bake_minutes=bake, success_criteria=crit)
            for i, (w, bake, crit) in enumerate(STAGES)
            if w >= weight or i == len(STAGES) - 1
        ]
        plan = GatewayPlan(
            routes=routes,
            stages=stages,
            rationale=f"Start at {weight}% canary per route; promote per stage only when success criteria hold; "
            "outlier detection + canary_controller roll back to 100% monolith on error-rate breach.",
        )
        self.bb.put(Keys.GATEWAY_PLAN, plan)

        envoy = self._envoy_config(plan, contracts)
        self.write_artifact("gateway/envoy.yaml", yaml.safe_dump(envoy, sort_keys=False))
        self.write_artifact("gateway/canary_controller.py", render("canary_controller.py.j2", plan=plan))
        self.write_artifact("reports/canary_migration_plan.md", self._plan_markdown(plan, contracts))
        self.write_artifact("reports/gateway_plan.json", plan)
        return self.ok(
            f"{len(routes)} canary route(s) at {weight}% across {len(stages)} stage(s)",
            routes=len(routes),
            canary_weight=weight,
            services=sorted({r.service for r in routes}),
        )

    # ---- envoy ----------------------------------------------------------------------

    @staticmethod
    def _envoy_config(plan: GatewayPlan, contracts: ContractPlan) -> dict[str, Any]:
        def cluster(name: str, host: str, port: int) -> dict[str, Any]:
            return {
                "name": name,
                "type": "STRICT_DNS",
                "connect_timeout": "2s",
                "lb_policy": "ROUND_ROBIN",
                "outlier_detection": {
                    "consecutive_5xx": 3,
                    "interval": "10s",
                    "base_ejection_time": "60s",
                    "max_ejection_percent": 100,
                },
                "load_assignment": {
                    "cluster_name": name,
                    "endpoints": [{"lb_endpoints": [{"endpoint": {"address": {"socket_address": {"address": host, "port_value": port}}}}]}],
                },
            }

        envoy_routes: list[dict[str, Any]] = []
        for r in plan.routes:
            # Convert OpenAPI {param} into a safe_regex so Envoy matches the concrete path.
            regex = "^" + "".join("[^/]+" if part.startswith("{") else part for part in _split_keep(r.path)) + "$"
            envoy_routes.append(
                {
                    "name": f"{r.method.lower()}_{r.path.strip('/').replace('/', '_').replace('{', '').replace('}', '')}",
                    "match": {
                        "safe_regex": {"regex": regex},
                        "headers": [{"name": ":method", "string_match": {"exact": r.method}}],
                    },
                    "route": {
                        "weighted_clusters": {
                            "clusters": [
                                {"name": "monolith", "weight": r.monolith_weight},
                                {"name": r.service, "weight": r.canary_weight},
                            ]
                        },
                        "retry_policy": {"retry_on": "connect-failure,refused-stream,5xx", "num_retries": 2},
                        "timeout": "15s",
                    },
                }
            )
        envoy_routes.append({"name": "default_monolith", "match": {"prefix": "/"}, "route": {"cluster": "monolith"}})

        monolith_host, monolith_port = plan.monolith_upstream.split(":")
        return {
            "static_resources": {
                "listeners": [
                    {
                        "name": "ingress",
                        "address": {"socket_address": {"address": "0.0.0.0", "port_value": plan.listen_port}},
                        "filter_chains": [
                            {
                                "filters": [
                                    {
                                        "name": "envoy.filters.network.http_connection_manager",
                                        "typed_config": {
                                            "@type": "type.googleapis.com/envoy.extensions.filters.network.http_connection_manager.v3.HttpConnectionManager",
                                            "stat_prefix": "ingress_http",
                                            "codec_type": "AUTO",
                                            "generate_request_id": True,
                                            "tracing": {"provider": {"name": "envoy.tracers.opentelemetry"}},
                                            "route_config": {
                                                "name": "strangler_fig",
                                                "virtual_hosts": [{"name": "shop", "domains": ["*"], "routes": envoy_routes}],
                                            },
                                            "http_filters": [
                                                {
                                                    "name": "envoy.filters.http.router",
                                                    "typed_config": {"@type": "type.googleapis.com/envoy.extensions.filters.http.router.v3.Router"},
                                                }
                                            ],
                                        },
                                    }
                                ]
                            }
                        ],
                    }
                ],
                "clusters": [
                    cluster("monolith", monolith_host, int(monolith_port)),
                    *[cluster(svc, svc, c.port) for svc, c in contracts.services.items()],
                ],
            },
            "admin": {"address": {"socket_address": {"address": "0.0.0.0", "port_value": 9901}}},
        }

    @staticmethod
    def _plan_markdown(plan: GatewayPlan, contracts: ContractPlan) -> str:
        lines = [
            "# Canary Migration Plan (Strangler Fig)",
            "",
            f"Gateway: **{plan.gateway}** on :{plan.listen_port} - default upstream `{plan.monolith_upstream}`.",
            f"Automatic rollback when 5xx rate > {plan.rollback_error_rate_threshold:.2%} (see `gateway/canary_controller.py`).",
            "",
            "## Routes",
            "",
            "| Method | Path | New service | Canary % | Monolith % |",
            "|---|---|---|---|---|",
        ]
        for r in plan.routes:
            lines.append(f"| {r.method} | `{r.path}` | {r.service} (:{contracts.services[r.service].port}) | {r.canary_weight} | {r.monolith_weight} |")
        lines += ["", "## Stages", "", "| Stage | Canary % | Bake | Promote when |", "|---|---|---|---|"]
        for s in plan.stages:
            lines.append(f"| {s.stage} | {s.canary_weight} | {s.bake_minutes} min | {s.success_criteria} |")
        lines += [
            "",
            "## Runbook",
            "",
            "1. `docker compose up --build` - monolith, gateway and services come up side by side.",
            "2. Point clients at the gateway (:8080). Nothing changes for them.",
            "3. Run the parity suite against gateway vs monolith (`reposplit parity ...`).",
            "4. Promote a stage by editing the weights in `gateway/envoy.yaml` (or let `canary_controller.py` do it).",
            "5. Roll back at any time: set canary weight to 0 - no code deploy required.",
        ]
        return "\n".join(lines) + "\n"


def _split_keep(path: str) -> list[str]:
    """Split '/a/{id}/b' into ['/a/', '{id}', '/b'] keeping separators."""
    parts: list[str] = []
    buf = ""
    i = 0
    while i < len(path):
        if path[i] == "{":
            if buf:
                parts.append(buf)
                buf = ""
            j = path.index("}", i)
            parts.append(path[i : j + 1])
            i = j + 1
        else:
            buf += path[i]
            i += 1
    if buf:
        parts.append(buf)
    return parts
