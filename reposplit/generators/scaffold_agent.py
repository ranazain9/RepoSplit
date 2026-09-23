"""Scaffold step - writes runnable services, the gateway compose topology and the Helm chart.

Not one of the six reasoning agents; it is the Supervisor's "spin up microservices" step and is
re-run inside the auto-healing loop after the Parity agent patches a ported function.
"""

from __future__ import annotations

import os
from pathlib import Path

from reposplit.core.base_agent import BaseAgent
from reposplit.core.schemas import (
    AgentResult,
    ContractPlan,
    DataPartitionPlan,
    DependencyGraph,
    DomainTopology,
    GatewayPlan,
    Keys,
    Phase,
    ScaffoldedService,
    ScaffoldManifest,
)
from reposplit.generators.porting import FlaskPorter, PortedFunction
from reposplit.generators.render import render


class ScaffoldAgent(BaseAgent):
    name = "scaffold"
    phase = Phase.SCAFFOLD
    requires = (Keys.DEPENDENCY_GRAPH, Keys.TOPOLOGY, Keys.DATA_PLAN, Keys.CONTRACT_PLAN, Keys.GATEWAY_PLAN)
    produces = (Keys.SCAFFOLD_MANIFEST,)
    uses_llm = False
    description = "Ports each cluster into a runnable FastAPI service; emits Docker Compose + OpenShift Helm chart."

    async def execute(self) -> AgentResult:
        graph = self.bb.require(Keys.DEPENDENCY_GRAPH, DependencyGraph)
        topology = self.bb.require(Keys.TOPOLOGY, DomainTopology)
        data_plan = self.bb.require(Keys.DATA_PLAN, DataPartitionPlan)
        contracts = self.bb.require(Keys.CONTRACT_PLAN, ContractPlan)
        gateway = self.bb.require(Keys.GATEWAY_PLAN, GatewayPlan)
        nodes = graph.node_map()
        ports = {svc: c.port for svc, c in contracts.services.items()}
        previous = self.bb.get(Keys.SCAFFOLD_MANIFEST, ScaffoldManifest)

        scaffolded: dict[str, ScaffoldedService] = {}
        for svc, contract in contracts.services.items():
            porter = FlaskPorter(self.ctx.repo_root, graph, topology, contracts, data_plan, svc)
            cluster = topology.clusters[svc]
            fn_nodes = [nodes[s] for s in cluster.symbols if nodes[s].kind == "function"]
            fn_nodes.sort(key=lambda n: (n.module, n.lineno))

            routes: list[PortedFunction] = []
            helpers: list[PortedFunction] = []
            notes: list[str] = []
            for n in fn_nodes:
                ported = porter.port_function(n)
                notes += ported.notes
                (routes if ported.kind == "route" else helpers).append(ported)
            seed = self._port_seed(porter, graph)
            if seed:
                notes += seed.notes
            imports, constants = porter.module_preamble(sorted({n.module for n in fn_nodes}))
            rpcs = [ep for ep in contract.endpoints if ep.exposure == "internal"]
            model_names = porter.owned_models

            base = f"services/{svc}"
            files = [
                self._write(f"{base}/app/__init__.py", ""),
                self._write(
                    f"{base}/app/main.py",
                    render(
                        "service/main.py.j2",
                        service=svc,
                        port=contract.port,
                        downstream=contract.downstream,
                        symbols=[n.name for n in fn_nodes] + model_names,
                        imports=imports,
                        constants=constants,
                        models=model_names,
                        seed=seed,
                        helpers=helpers,
                        routes=routes,
                        rpcs=rpcs,
                    ),
                    healed=self._healed_files(previous, svc),
                ),
                self._write(f"{base}/app/db.py", render("service/db.py.j2", service=svc)),
                self._write(
                    f"{base}/app/middleware.py",
                    render("service/middleware.py.j2", service=svc, headers=contracts.propagated_headers),
                ),
                self._write(
                    f"{base}/app/clients.py",
                    render(
                        "service/clients.py.j2",
                        service=svc,
                        downstream=contract.downstream,
                        rpcs={d: [e for e in contracts.services[d].endpoints if e.exposure == "internal"] for d in contract.downstream},
                        ports=ports,
                    ),
                ),
                self._write(f"{base}/app/models.py", self._models_source(svc)),
                self._write(f"{base}/app/outbox.py", self._outbox_source()),
                self._write(f"{base}/app/legacy_reference.py", self._legacy_reference(porter, fn_nodes, svc)),
                self._write(f"{base}/Dockerfile", render("service/Dockerfile.j2", port=contract.port)),
                self._write(f"{base}/requirements.txt", render("service/requirements.txt.j2")),
            ]
            scaffolded[svc] = ScaffoldedService(
                service=svc,
                directory=base,
                port=contract.port,
                files=files,
                routes=len(routes),
                rpcs=len(rpcs),
                porting_notes=notes,
            )
            if notes:
                self.log(f"{svc}: {len(notes)} porting note(s)", level="warning", notes=notes)

        try:
            monolith_context = os.path.relpath(self.ctx.repo_root, self.ctx.output_dir).replace("\\", "/")
        except ValueError:
            monolith_context = Path(self.ctx.repo_root).resolve().as_posix()
        compose = self._write(
            "docker-compose.yml",
            render(
                "docker-compose.yml.j2",
                monolith_context=monolith_context,
                gateway_port=gateway.listen_port,
                services=list(contracts.services.values()),
                ports=ports,
            ),
        )
        release = "reposplit-modernized"
        helm_ctx = {"release": release, "gateway_port": gateway.listen_port, "services": list(contracts.services.values()), "ports": ports}
        self._write("helm/reposplit/Chart.yaml", render("helm/Chart.yaml.j2", **helm_ctx))
        self._write("helm/reposplit/values.yaml", render("helm/values.yaml.j2", **helm_ctx))
        self._write("helm/reposplit/templates/deployment.yaml", render("helm/deployment.yaml.j2", **helm_ctx))
        self._write("helm/reposplit/templates/route.yaml", render("helm/route.yaml.j2", **helm_ctx))

        from reposplit.generators.cdc import CDCGenerator

        cdc_gen = CDCGenerator(self.ctx.output_dir, data_plan, topology)
        for cdc_file in cdc_gen.generate():
            self.bb.register_artifact(cdc_file)

        manifest = ScaffoldManifest(
            services=scaffolded, compose_file=compose, helm_chart="helm/reposplit", monolith_context=monolith_context
        )
        self.bb.put(Keys.SCAFFOLD_MANIFEST, manifest)
        self.write_artifact("reports/scaffold_manifest.json", manifest)
        total_notes = sum(len(s.porting_notes) for s in scaffolded.values())
        return self.ok(
            f"{len(scaffolded)} service(s) scaffolded, {sum(s.routes for s in scaffolded.values())} route(s), "
            f"{sum(s.rpcs for s in scaffolded.values())} RPC(s), {total_notes} porting note(s)",
            services={k: {"routes": v.routes, "rpcs": v.rpcs, "notes": len(v.porting_notes)} for k, v in scaffolded.items()},
        )

    # ---- helpers -------------------------------------------------------------------

    def _write(self, relpath: str, content: str, healed: set[str] | None = None) -> str:
        """Write unless the auto-healer already patched this file in a previous iteration."""
        target = self.ctx.output_dir / relpath
        if healed and relpath in healed and target.exists():
            self.bb.register_artifact(relpath)
            return relpath
        self.write_artifact(relpath, content)
        return relpath

    @staticmethod
    def _healed_files(previous: ScaffoldManifest | None, svc: str) -> set[str]:
        if previous is None or svc not in previous.services:
            return set()
        return {n.removeprefix("healed:") for n in previous.services[svc].porting_notes if n.startswith("healed:")}

    def _port_seed(self, porter: FlaskPorter, graph: DependencyGraph) -> PortedFunction | None:
        for n in graph.nodes:
            if n.kind == "function" and n.name.startswith("seed"):
                ported = porter.port_seed(n)
                if ported:
                    return ported
        return None

    def _models_source(self, svc: str) -> str:
        path = self.ctx.output_dir / "data" / "isolated_schemas" / svc / "models.py"
        if path.exists():
            return path.read_text(encoding="utf-8")
        return '"""No owned tables."""\nfrom sqlalchemy.orm import declarative_base\n\nBase = declarative_base()\n'

    @staticmethod
    def _legacy_reference(porter: FlaskPorter, fn_nodes, svc: str) -> str:
        parts = [
            f'"""Verbatim monolith sources for {svc} (read-only reference for reviewers and the auto-healer)."""',
            "# ruff: noqa",
            "# fmt: off",
            "",
        ]
        for n in fn_nodes:
            parts.append(f"# ---- {n.id} (lines {n.lineno}-{n.end_lineno}) ----")
            parts.append(porter.function_source(n, with_decorators=True))
            parts.append("")
        return "\n".join(parts)

    @property
    def output(self) -> Path:
        return self.ctx.output_dir

    @staticmethod
    def _outbox_source() -> str:
        return (
            '"""Transactional Outbox event publisher."""\n\n'
            "from __future__ import annotations\n\n"
            "import logging\n"
            "from typing import Any\n\n"
            'logger = logging.getLogger("reposplit.outbox")\n\n\n'
            "def publish_event(event_type: str, payload: dict[str, Any]) -> None:\n"
            '    logger.info("Outbox event published: %s -> %s", event_type, payload.get("id"))\n'
        )

