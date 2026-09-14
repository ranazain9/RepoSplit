"""Mechanical Flask -> FastAPI porter.

Moves each cluster's functions into its new service and rewrites the well-known Flask idioms:

    request.get_json()          -> body (FastAPI Body)
    jsonify(x), 201             -> JSONResponse(content=x, status_code=201)
    abort(404, description=..)  -> raise HTTPException(status_code=404, detail=..)
    db.session.*                -> session.*   (module-level scoped_session)
    Model.query.get(x)          -> session.get(Model, x)
    Model.query.filter_by(..)   -> session.query(Model).filter_by(..)
    severed_call(..)            -> clients.<owner_service>.severed_call(..)
    ForeignModel.query.get(x)   -> clients.<owner_service>.get_foreign_model(x)

Anything it cannot prove safe is left untouched and reported as a porting note, which becomes the
input of the Test & Parity agent's auto-healing loop. This is deliberately a scaffold: it ports the
80% of idioms that appear in most Flask monoliths and makes the remaining 20% visible.
"""

from __future__ import annotations

import ast
import re
import textwrap
from dataclasses import dataclass, field
from pathlib import Path

from reposplit.agents.contract.generators import flask_path_to_openapi
from reposplit.core.schemas import ContractPlan, DataPartitionPlan, DependencyGraph, DomainTopology, GraphNode
from reposplit.utils.naming import to_snake

_CONVERTER_TYPES = {"int": "int", "float": "float", "string": "str", "path": "str", "uuid": "str", None: "str"}
_FLASK_IMPORT_RE = re.compile(r"^\s*from\s+flask(_sqlalchemy)?\s+import|^\s*import\s+flask")


@dataclass
class PortedFunction:
    name: str
    kind: str  # route | helper | seed
    source: str
    methods: list[str] = field(default_factory=list)
    path: str = ""
    notes: list[str] = field(default_factory=list)


class FlaskPorter:
    def __init__(
        self,
        repo_root: Path,
        graph: DependencyGraph,
        topology: DomainTopology,
        contracts: ContractPlan,
        data_plan: DataPartitionPlan,
        service: str,
    ) -> None:
        self.repo_root = repo_root
        self.graph = graph
        self.nodes = graph.node_map()
        self.topology = topology
        self.service = service
        cluster = topology.clusters[service]
        self.owned_symbols = set(cluster.symbols)
        self.owned_models = sorted(self.nodes[s].name for s in cluster.symbols if self.nodes[s].kind == "class" and self.nodes[s].is_model)
        self.foreign_models: dict[str, str] = {}
        for other, c in topology.clusters.items():
            if other == service:
                continue
            for s in c.symbols:
                n = self.nodes[s]
                if n.kind == "class" and n.is_model:
                    self.foreign_models[n.name] = other
        # callee function name -> owning service, for calls that cross this service's boundary
        self.callee_service: dict[str, str] = {}
        for e in topology.severed_edges:
            if e.source_cluster == service and e.kind == "call":
                self.callee_service[self.nodes[e.target].name] = e.target_cluster
        self.internal_ops: dict[str, set[str]] = {}
        for svc, contract in contracts.services.items():
            self.internal_ops[svc] = {ep.operation_id for ep in contract.endpoints if ep.exposure == "internal"}
        self._file_cache: dict[str, tuple[str, ast.Module]] = {}

    # ---- source access ---------------------------------------------------------------

    def _module(self, rel: str) -> tuple[str, ast.Module]:
        if rel not in self._file_cache:
            src = (self.repo_root / rel).read_text(encoding="utf-8", errors="ignore")
            self._file_cache[rel] = (src, ast.parse(src))
        return self._file_cache[rel]

    def function_source(self, node: GraphNode, with_decorators: bool = False) -> str:
        src, tree = self._module(node.module)
        lines = src.splitlines()
        for fn in tree.body:
            if isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef) and fn.name == node.name:
                start = min([fn.lineno] + [d.lineno for d in fn.decorator_list]) if with_decorators else fn.lineno
                return textwrap.dedent("\n".join(lines[start - 1 : fn.end_lineno]))
        raise KeyError(f"{node.name} not found in {node.module}")

    def module_preamble(self, modules: list[str]) -> tuple[list[str], list[str]]:
        """External imports and module-level constants the ported functions depend on."""
        imports: list[str] = []
        constants: list[str] = []
        for rel in modules:
            src, tree = self._module(rel)
            lines = src.splitlines()
            for stmt in tree.body:
                text = "\n".join(lines[stmt.lineno - 1 : stmt.end_lineno])
                if isinstance(stmt, ast.Import | ast.ImportFrom):
                    if _FLASK_IMPORT_RE.match(text) or self._is_repo_import(stmt):
                        continue
                    if text not in imports:
                        imports.append(text)
                elif isinstance(stmt, ast.Assign | ast.AnnAssign):
                    value = stmt.value
                    if value is None:
                        continue
                    vsrc = ast.unparse(value)
                    if "Blueprint(" in vsrc or "SQLAlchemy(" in vsrc or "Flask(" in vsrc:
                        continue
                    if text not in constants:
                        constants.append(text)
        return imports, constants

    def _is_repo_import(self, stmt: ast.Import | ast.ImportFrom) -> bool:
        repo_modules = {n.name for n in self.graph.nodes if n.kind == "module"}
        top_levels = {m.split(".")[0] for m in repo_modules}
        if isinstance(stmt, ast.ImportFrom):
            return stmt.level > 0 or (stmt.module or "").split(".")[0] in top_levels
        return any(a.name.split(".")[0] in top_levels for a in stmt.names)

    # ---- porting -----------------------------------------------------------------------

    def port_function(self, node: GraphNode) -> PortedFunction:
        raw = self.function_source(node)
        notes: list[str] = []
        body = raw
        uses_body = "request.get_json(" in body or "request.json" in body

        # signature ---------------------------------------------------------------
        header_match = re.match(r"def\s+(\w+)\((.*?)\)\s*(->\s*[^:]+)?:", body, flags=re.S)
        if not header_match:
            raise ValueError(f"cannot parse signature of {node.name}")
        params = [p.strip() for p in header_match.group(2).split(",") if p.strip()]
        path = ""
        methods: list[str] = []
        if node.route:
            path, path_params = flask_path_to_openapi(node.route.path)
            methods = node.route.methods
            conv = {m.group("name"): m.group("conv") for m in re.finditer(r"<(?:(?P<conv>\w+):)?(?P<name>\w+)>", node.route.path)}
            params = [f"{p.split(':')[0].split('=')[0].strip()}: {_CONVERTER_TYPES[conv.get(p.split(':')[0].split('=')[0].strip())]}" for p in params]
            if uses_body:
                params.append("body: dict = Body(default={})")
        new_header = f"def {node.name}({', '.join(params)}):"
        body = body[: header_match.start()] + new_header + body[header_match.end() :]

        # flask idioms ------------------------------------------------------------
        body = re.sub(r"request\.get_json\([^)]*\)", "body", body)
        body = body.replace("request.json", "body")
        body = re.sub(r"return jsonify\((.+)\),\s*(\d{3})\s*$", r"return JSONResponse(content=\1, status_code=\2)", body, flags=re.M)
        body = re.sub(r"return jsonify\((.+)\)\s*$", r"return \1", body, flags=re.M)
        body = re.sub(r"abort\((\d{3}),\s*description=", r"raise HTTPException(status_code=\1, detail=", body)
        body = re.sub(r"abort\((\d{3})\)", r"raise HTTPException(status_code=\1)", body)
        if "jsonify(" in body:
            notes.append(f"{node.name}: multi-line jsonify() left in place")
        if "request." in body:
            notes.append(f"{node.name}: uses flask `request` attributes beyond JSON body (args/headers/files)")

        # data access -------------------------------------------------------------
        body = body.replace("db.session.", "session.")
        for model, owner in self.foreign_models.items():
            op = f"get_{to_snake(model)}"
            if op in self.internal_ops.get(owner, set()):
                body = re.sub(rf"\b{model}\.query\.get\(", f"clients.{owner}.{op}(", body)
        body = re.sub(r"\b(\w+)\.query\.get\(", r"session.get(\1, ", body)
        body = re.sub(r"\b(\w+)\.query\.", r"session.query(\1).", body)

        # severed calls -----------------------------------------------------------
        for callee, owner in self.callee_service.items():
            body = re.sub(rf"(?<![\w.]){callee}\(", f"clients.{owner}.{callee}(", body)

        # anything still referencing a foreign model needs a projection or a client call
        for model, owner in self.foreign_models.items():
            if re.search(rf"\b{model}\b", body):
                notes.append(
                    f"{node.name}: still references foreign model {model} (owned by {owner}) - "
                    "cross-service JOIN: read from the CQRS projection or add a client call"
                )
                body = body.replace(new_header, f"{new_header}\n    # TODO(auto-heal): {notes[-1]}", 1)

        return PortedFunction(name=node.name, kind="route" if node.route else "helper", source=body, methods=methods, path=path, notes=notes)

    def port_seed(self, node: GraphNode) -> PortedFunction | None:
        """Port a shared-kernel seed function, keeping only rows for the models this service owns."""
        raw = self.function_source(node)
        if not any(re.search(rf"\b{m}\b", raw) for m in self.owned_models):
            return None
        out: list[str] = []
        first_owned = self.owned_models[0]
        for line in raw.splitlines():
            foreign_here = [m for m in self.foreign_models if re.search(rf"\b{m}\b", line)]
            if foreign_here and "session.add(" in line.replace("db.session", "session"):
                continue
            for m in foreign_here:
                line = re.sub(rf"\b{m}\.query\b", f"{first_owned}.query", line)
            line = line.replace("db.session.", "session.")
            line = re.sub(r"\b(\w+)\.query\.", r"session.query(\1).", line)
            out.append(line)
        src = "\n".join(out).replace(f"def {node.name}(", "def seed(", 1)
        return PortedFunction(name="seed", kind="seed", source=src, notes=[f"seed ported from {node.id}; review rows"])
