"""Whole-repository Python AST parser -> DependencyGraph.

Nodes: modules, classes, top-level functions (methods are recorded but collapsed into their
class for partitioning). Edges: import (module->module), call (symbol->symbol), data_access
(symbol->ORM model class), fk (model->model via ForeignKey/relationship), inherits.

Only Python is implemented; `LanguageParser` is the extension point for JS/Go (tree-sitter).
"""

from __future__ import annotations

import ast
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from reposplit.core.schemas import DependencyGraph, GraphEdge, GraphNode, ParamSpec, RouteInfo
from reposplit.utils.hashing import iter_repo_files

MODEL_BASES = {"Model", "db.Model", "Base", "DeclarativeBase", "SQLModel", "Document"}
ROUTE_DECORATOR_ATTRS = {"route", "get", "post", "put", "patch", "delete"}
SKIP_DIRS = {"tests", "test", "migrations", "alembic", "scripts"}


class LanguageParser(Protocol):
    def parse(self) -> DependencyGraph: ...


@dataclass
class _ModuleInfo:
    rel: str
    dotted: str
    tree: ast.Module
    source: str
    symbols: dict[str, str] = field(default_factory=dict)  # local name -> node id
    imports: dict[str, tuple[str, str]] = field(default_factory=dict)  # local name -> (kind, target)


def _decorator_name(dec: ast.expr) -> str:
    target = dec.func if isinstance(dec, ast.Call) else dec
    return ast.unparse(target)


def _route_from_decorators(decorators: list[ast.expr]) -> RouteInfo | None:
    for dec in decorators:
        if not isinstance(dec, ast.Call) or not isinstance(dec.func, ast.Attribute):
            continue
        attr = dec.func.attr
        if attr not in ROUTE_DECORATOR_ATTRS:
            continue
        if not dec.args or not isinstance(dec.args[0], ast.Constant) or not isinstance(dec.args[0].value, str):
            continue
        path = dec.args[0].value
        methods = ["GET"] if attr in ("route", "get") else [attr.upper()]
        for kw in dec.keywords:
            if kw.arg == "methods" and isinstance(kw.value, ast.List | ast.Tuple):
                methods = [
                    elt.value.upper() for elt in kw.value.elts if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
                ]
        return RouteInfo(path=path, methods=methods, blueprint=ast.unparse(dec.func.value))
    return None


def _params(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ParamSpec]:
    args = fn.args
    positional = args.posonlyargs + args.args
    defaults: list[ast.expr | None] = [None] * (len(positional) - len(args.defaults)) + list(args.defaults)
    specs: list[ParamSpec] = []
    for arg, default in zip(positional, defaults, strict=True):
        if arg.arg in ("self", "cls"):
            continue
        specs.append(
            ParamSpec(
                name=arg.arg,
                type_hint=ast.unparse(arg.annotation) if arg.annotation else None,
                required=default is None,
                default=ast.unparse(default) if default is not None else None,
            )
        )
    for arg, default in zip(args.kwonlyargs, args.kw_defaults, strict=True):
        specs.append(
            ParamSpec(
                name=arg.arg,
                type_hint=ast.unparse(arg.annotation) if arg.annotation else None,
                required=default is None,
                default=ast.unparse(default) if default is not None else None,
            )
        )
    return specs


def _body_keys(fn: ast.AST) -> list[str]:
    """Keys read from the JSON request body: data = request.get_json(); data["x"] / data.get("x")."""
    body_vars: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            value = node.value
            src = ast.unparse(value)
            if src.startswith("request.get_json(") or src in ("request.json", "request.get_json()", "await request.json()"):
                body_vars.add(node.targets[0].id)
    found: list[tuple[int, int, str]] = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id in body_vars:
            if isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
                found.append((node.lineno, node.col_offset, node.slice.value))
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in body_vars
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            found.append((node.lineno, node.col_offset, node.args[0].value))
    return list(dict.fromkeys(key for _, _, key in sorted(found)))  # source order


class PythonRepoParser:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.modules: dict[str, _ModuleInfo] = {}
        self.by_dotted: dict[str, str] = {}
        self.nodes: dict[str, GraphNode] = {}
        self.edges: dict[tuple[str, str, str], int] = defaultdict(int)
        self.tables: dict[str, str] = {}  # table name -> class node id

    # ---- public --------------------------------------------------------------------

    def parse(self) -> DependencyGraph:
        files = [
            f
            for f in iter_repo_files(self.root, suffixes={".py"})
            if not any(part in SKIP_DIRS for part in f.relative_to(self.root).parts)
        ]
        total_loc = 0
        for path in files:
            source = path.read_text(encoding="utf-8", errors="ignore")
            try:
                tree = ast.parse(source)
            except SyntaxError:
                continue
            rel = path.relative_to(self.root).as_posix()
            dotted = rel[:-3].replace("/", ".")
            dotted = dotted.removesuffix(".__init__")
            info = _ModuleInfo(rel=rel, dotted=dotted, tree=tree, source=source)
            self.modules[rel] = info
            self.by_dotted[dotted] = rel
            loc = source.count("\n") + 1
            total_loc += loc
            self.nodes[rel] = GraphNode(id=rel, kind="module", module=rel, name=dotted, loc=loc)

        for info in self.modules.values():
            self._collect_symbols(info)
        for info in self.modules.values():
            self._collect_imports(info)
        for info in self.modules.values():
            self._collect_edges(info)

        edges = [
            GraphEdge(source=s, target=t, kind=k, weight=w)  # type: ignore[arg-type]
            for (s, t, k), w in sorted(self.edges.items())
        ]
        return DependencyGraph(
            root=str(self.root),
            nodes=sorted(self.nodes.values(), key=lambda n: n.id),
            edges=edges,
            file_count=len(self.modules),
            total_loc=total_loc,
        )

    # ---- pass 1: symbols -----------------------------------------------------------

    def _collect_symbols(self, info: _ModuleInfo) -> None:
        for node in info.tree.body:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                nid = f"{info.rel}::{node.name}"
                info.symbols[node.name] = nid
                self.nodes[nid] = GraphNode(
                    id=nid,
                    kind="function",
                    module=info.rel,
                    name=node.name,
                    lineno=node.lineno,
                    end_lineno=node.end_lineno or node.lineno,
                    loc=(node.end_lineno or node.lineno) - node.lineno + 1,
                    params=_params(node),
                    returns=ast.unparse(node.returns) if node.returns else None,
                    route=_route_from_decorators(node.decorator_list),
                    decorators=[_decorator_name(d) for d in node.decorator_list],
                    body_keys=_body_keys(node),
                )
            elif isinstance(node, ast.ClassDef):
                nid = f"{info.rel}::{node.name}"
                info.symbols[node.name] = nid
                bases = [ast.unparse(b) for b in node.bases]
                is_model = any(b in MODEL_BASES or b.endswith(".Model") for b in bases)
                table = None
                for stmt in node.body:
                    if (
                        isinstance(stmt, ast.Assign)
                        and any(isinstance(t, ast.Name) and t.id == "__tablename__" for t in stmt.targets)
                        and isinstance(stmt.value, ast.Constant)
                    ):
                        table = str(stmt.value.value)
                if is_model and table is None:
                    table = node.name.lower() + "s"
                if is_model and table:
                    self.tables[table] = nid
                self.nodes[nid] = GraphNode(
                    id=nid,
                    kind="class",
                    module=info.rel,
                    name=node.name,
                    lineno=node.lineno,
                    end_lineno=node.end_lineno or node.lineno,
                    loc=(node.end_lineno or node.lineno) - node.lineno + 1,
                    is_model=is_model,
                    table=table,
                    bases=bases,
                    decorators=[_decorator_name(d) for d in node.decorator_list],
                )
                for stmt in node.body:
                    if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef):
                        mid = f"{nid}.{stmt.name}"
                        self.nodes[mid] = GraphNode(
                            id=mid,
                            kind="method",
                            module=info.rel,
                            name=f"{node.name}.{stmt.name}",
                            lineno=stmt.lineno,
                            end_lineno=stmt.end_lineno or stmt.lineno,
                            loc=(stmt.end_lineno or stmt.lineno) - stmt.lineno + 1,
                            params=_params(stmt),
                            returns=ast.unparse(stmt.returns) if stmt.returns else None,
                            decorators=[_decorator_name(d) for d in stmt.decorator_list],
                        )

    # ---- pass 2: imports -----------------------------------------------------------

    def _resolve_module(self, dotted: str | None, level: int, current: _ModuleInfo) -> str | None:
        if level > 0:
            parts = current.dotted.split(".")
            base = parts[: max(len(parts) - level, 0)] if current.rel.endswith("__init__.py") else parts[:-level]
            dotted = ".".join(base + [dotted]) if dotted else ".".join(base)
        if not dotted:
            return None
        return self.by_dotted.get(dotted)

    def _collect_imports(self, info: _ModuleInfo) -> None:
        for node in ast.walk(info.tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    rel = self._resolve_module(alias.name, 0, info)
                    if rel:
                        local = alias.asname or alias.name.split(".")[0]
                        info.imports[local] = ("module", rel)
                        self.edges[(info.rel, rel, "import")] += 1
            elif isinstance(node, ast.ImportFrom):
                rel = self._resolve_module(node.module, node.level, info)
                for alias in node.names:
                    local = alias.asname or alias.name
                    if rel and alias.name in self.modules[rel].symbols:
                        info.imports[local] = ("symbol", self.modules[rel].symbols[alias.name])
                        self.edges[(info.rel, rel, "import")] += 1
                        continue
                    sub = self._resolve_module(f"{node.module}.{alias.name}" if node.module else alias.name, node.level, info)
                    if sub:
                        info.imports[local] = ("module", sub)
                        self.edges[(info.rel, sub, "import")] += 1
                    elif rel:
                        info.imports[local] = ("module_attr", rel)
                        self.edges[(info.rel, rel, "import")] += 1

    # ---- pass 3: edges -------------------------------------------------------------

    def _resolve_name(self, name: str, info: _ModuleInfo) -> str | None:
        if name in info.symbols:
            return info.symbols[name]
        imp = info.imports.get(name)
        if imp and imp[0] == "symbol":
            return imp[1]
        return None

    def _resolve_attr_call(self, func: ast.Attribute, info: _ModuleInfo) -> str | None:
        """module_alias.func(...) -> symbol in that module."""
        if isinstance(func.value, ast.Name):
            imp = info.imports.get(func.value.id)
            if imp and imp[0] == "module":
                return self.modules[imp[1]].symbols.get(func.attr)
        return None

    def _collect_edges(self, info: _ModuleInfo) -> None:
        owners: list[tuple[str, ast.AST]] = []
        module_level: list[ast.stmt] = []
        for node in info.tree.body:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                owners.append((info.symbols[node.name], node))
            else:
                module_level.append(node)
        for owner, node in owners:
            self._scan_body(owner, node, info)
            if isinstance(node, ast.ClassDef):
                self._scan_class(owner, node, info)
        for stmt in module_level:
            self._scan_body(info.rel, stmt, info)

    def _scan_class(self, owner: str, node: ast.ClassDef, info: _ModuleInfo) -> None:
        for base in node.bases:
            target = self._resolve_name(ast.unparse(base).split(".")[-1], info)
            if target and target != owner:
                self.edges[(owner, target, "inherits")] += 1
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Call):
                continue
            fname = ast.unparse(sub.func).split(".")[-1]
            if fname == "ForeignKey" and sub.args and isinstance(sub.args[0], ast.Constant):
                table = str(sub.args[0].value).split(".")[0]
                target = self.tables.get(table)
                if target and target != owner:
                    self.edges[(owner, target, "fk")] += 1
            elif fname == "relationship" and sub.args and isinstance(sub.args[0], ast.Constant):
                target = self._resolve_name(str(sub.args[0].value), info)
                if target and target != owner:
                    self.edges[(owner, target, "fk")] += 1

    def _scan_body(self, owner: str, node: ast.AST, info: _ModuleInfo) -> None:
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                target = None
                if isinstance(sub.func, ast.Name):
                    target = self._resolve_name(sub.func.id, info)
                elif isinstance(sub.func, ast.Attribute):
                    target = self._resolve_attr_call(sub.func, info)
                # Model constructors (User(...)) are counted by the Name branch below.
                if target and target != owner and not self.nodes[target].is_model:
                    self.edges[(owner, target, "call")] += 1
            elif isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
                target = self._resolve_name(sub.id, info)
                if target and target != owner and self.nodes[target].is_model:
                    # Model referenced as a value (User.query, session.query(User), User(...)).
                    # Constructor calls are also caught above; count once per reference site.
                    self.edges[(owner, target, "data_access")] += 1
