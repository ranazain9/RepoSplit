"""Contract Agent - Phase 3: severed in-memory calls -> strongly typed network contracts.

Deterministic by design: contracts are derived from AST signatures and the topology, so two runs on
the same commit produce byte-identical OpenAPI/proto files (important for the Migration Passport).
"""

from __future__ import annotations

from collections import defaultdict

import yaml

from reposplit.agents.contract.generators import build_openapi, build_proto, flask_path_to_openapi
from reposplit.core.base_agent import BaseAgent
from reposplit.core.schemas import (
    AgentResult,
    ContractPlan,
    DependencyGraph,
    DomainTopology,
    EndpointContract,
    Keys,
    ParamSpec,
    Phase,
    ServiceContract,
)
from reposplit.utils.naming import service_port, service_short, to_snake


class ContractAgent(BaseAgent):
    name = "contract"
    phase = Phase.CONTRACT
    requires = (Keys.DEPENDENCY_GRAPH, Keys.TOPOLOGY, Keys.DATA_PLAN)
    produces = (Keys.CONTRACT_PLAN,)
    uses_llm = False
    description = "OpenAPI 3.0 + gRPC proto synthesis, typed clients, X-User-Id/X-Tenant-Id/traceparent propagation."

    async def execute(self) -> AgentResult:
        graph = self.bb.require(Keys.DEPENDENCY_GRAPH, DependencyGraph)
        topology = self.bb.require(Keys.TOPOLOGY, DomainTopology)
        nodes = graph.node_map()
        services = topology.service_clusters()

        endpoints: dict[str, list[EndpointContract]] = defaultdict(list)
        downstream: dict[str, set[str]] = defaultdict(set)

        # Public API: every routed function keeps its monolith path so the gateway can canary it 1:1.
        for svc, cluster in services.items():
            for sid in cluster.symbols:
                n = nodes[sid]
                if n.kind != "function" or not n.route:
                    continue
                path, path_params = flask_path_to_openapi(n.route.path)
                for method in n.route.methods:
                    endpoints[svc].append(
                        EndpointContract(
                            service=svc,
                            operation_id=n.name if len(n.route.methods) == 1 else f"{n.name}_{method.lower()}",
                            method=method,
                            path=path,
                            exposure="public",
                            params=n.params,
                            body_keys=n.body_keys,
                            path_params=[p for p, _ in path_params],
                            response_type=n.returns or "object",
                            response_fields=n.route.response_fields if n.route else [],
                            status_codes=n.route.status_codes if n.route else [],
                            source_symbol=sid,
                        )
                    )

        # Internal API: every severed call/data-access edge becomes an RPC on the owning service.
        seen: set[tuple[str, str]] = set()
        for edge in topology.severed_edges:
            src_svc, dst_svc = edge.source_cluster, edge.target_cluster
            if dst_svc not in services:
                continue
            target = nodes[edge.target]
            downstream[src_svc].add(dst_svc)
            key = (dst_svc, target.id)
            if key in seen:
                for ep in endpoints[dst_svc]:
                    if ep.source_symbol == target.id and src_svc not in ep.callers:
                        ep.callers.append(src_svc)
                continue
            seen.add(key)
            short = service_short(dst_svc)
            if edge.kind == "call" and target.kind == "function":
                resp_fields = target.route.response_fields if target.route else []
                status_codes = target.route.status_codes if target.route else [200]
                endpoints[dst_svc].append(
                    EndpointContract(
                        service=dst_svc,
                        operation_id=target.name,
                        method="POST",
                        path=f"/internal/v1/{short}/{target.name}",
                        exposure="internal",
                        params=target.params,
                        body_keys=[p.name for p in target.params],
                        response_type=target.returns or "object",
                        response_fields=resp_fields,
                        status_codes=status_codes,
                        source_symbol=target.id,
                        callers=[src_svc],
                    )
                )
            elif edge.kind in ("data_access", "fk") and target.kind == "class":
                entity = to_snake(target.name)
                endpoints[dst_svc].append(
                    EndpointContract(
                        service=dst_svc,
                        operation_id=f"get_{entity}",
                        method="GET",
                        path=f"/internal/v1/{short}/{entity}/{{id}}",
                        exposure="internal",
                        params=[ParamSpec(name="id", type_hint="int")],
                        path_params=["id"],
                        response_type=target.name,
                        response_fields=[],
                        status_codes=[200, 404],
                        source_symbol=target.id,
                        callers=[src_svc],
                    )
                )

        plan = ContractPlan(services={})
        for index, svc in enumerate(sorted(services)):
            eps = sorted(endpoints[svc], key=lambda e: (e.exposure, e.path, e.method))
            contract = ServiceContract(
                service=svc,
                port=service_port(index + 1),
                base_path=f"/api/v1/{service_short(svc)}",
                endpoints=eps,
                downstream=sorted(downstream[svc] & set(services)),
            )
            contract.openapi = build_openapi(contract, plan.propagated_headers)
            contract.proto = build_proto(contract)
            plan.services[svc] = contract
            self.write_artifact(f"contracts/{svc}/openapi.yaml", yaml.safe_dump(contract.openapi, sort_keys=False))
            self.write_artifact(f"contracts/{svc}/service.proto", contract.proto)
        plan.rationale = (
            "Public routes preserved verbatim for 1:1 canary routing; each severed call became an internal POST RPC, "
            "each severed data access a GET-by-id; security context propagated via headers/gRPC metadata."
        )
        self.bb.put(Keys.CONTRACT_PLAN, plan)
        self.write_artifact("reports/contract_plan.json", plan)

        totals = {svc: len(c.endpoints) for svc, c in plan.services.items()}
        internal = sum(1 for c in plan.services.values() for e in c.endpoints if e.exposure == "internal")
        return self.ok(
            f"{sum(totals.values())} endpoint(s) across {len(plan.services)} service(s) ({internal} internal RPCs)",
            endpoints=totals,
            internal_rpcs=internal,
            propagated_headers=plan.propagated_headers,
        )
