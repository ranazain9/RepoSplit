"""SQLAlchemy / Flask-SQLAlchemy and Django ORM model parsing (AST based, no import of target code)."""

from __future__ import annotations

import ast
import re
from pathlib import Path

from reposplit.core.schemas import ColumnSpec, DependencyGraph, EntityModel

COLUMN_CALLS = {"Column", "mapped_column"}
# Django ORM fields that map to DB columns (ManyToManyField is excluded — it creates a junction table)
DJANGO_FIELD_CALLS = {
    "AutoField", "BigAutoField", "SmallAutoField",
    "CharField", "TextField", "EmailField", "URLField", "SlugField",
    "IntegerField", "BigIntegerField", "SmallIntegerField", "PositiveIntegerField",
    "FloatField", "DecimalField",
    "BooleanField", "NullBooleanField",
    "DateField", "DateTimeField", "TimeField", "DurationField",
    "UUIDField", "JSONField", "BinaryField",
    "ForeignKey", "OneToOneField",
}
_FK_RE = re.compile(r",?\s*(?:db\.)?ForeignKey\((['\"])([^'\"]+)\1[^)]*\)")


def _call_name(call: ast.Call) -> str:
    return ast.unparse(call.func).split(".")[-1]


def _find_fk(call: ast.Call) -> str | None:
    for arg in list(call.args) + [kw.value for kw in call.keywords]:
        if isinstance(arg, ast.Call) and _call_name(arg) == "ForeignKey" and arg.args:
            first = arg.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                return first.value
    return None


def _find_ondelete(call: ast.Call) -> str | None:
    for arg in list(call.args) + [kw.value for kw in call.keywords]:
        if isinstance(arg, ast.Call) and _call_name(arg) == "ForeignKey":
            for kw in arg.keywords:
                if kw.arg == "ondelete" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                    return kw.value.value
    return None


def _kw_bool(call: ast.Call, name: str, default: bool) -> bool:
    for kw in call.keywords:
        if kw.arg == name and isinstance(kw.value, ast.Constant):
            return bool(kw.value.value)
    return default


def parse_entities(repo_root: Path, graph: DependencyGraph) -> list[EntityModel]:
    entities: list[EntityModel] = []
    by_module: dict[str, list] = {}
    for node in graph.nodes:
        if node.kind == "class" and node.is_model:
            by_module.setdefault(node.module, []).append(node)

    for module, class_nodes in sorted(by_module.items()):
        source = (repo_root / module).read_text(encoding="utf-8", errors="ignore")
        tree = ast.parse(source)
        wanted = {n.name: n for n in class_nodes}
        for cls in tree.body:
            if not isinstance(cls, ast.ClassDef) or cls.name not in wanted:
                continue
            gnode = wanted[cls.name]
            columns: list[ColumnSpec] = []
            relationships: list[str] = []
            for stmt in cls.body:
                target_name: str | None = None
                value: ast.expr | None = None
                if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
                    target_name, value = stmt.targets[0].id, stmt.value
                elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                    target_name, value = stmt.target.id, stmt.value
                if target_name is None or not isinstance(value, ast.Call):
                    continue
                cname = _call_name(value)
                if cname == "relationship" and value.args and isinstance(value.args[0], ast.Constant):
                    relationships.append(str(value.args[0].value))
                    continue
                # ---- SQLAlchemy Column / mapped_column ----
                if cname in COLUMN_CALLS:
                    type_expr = ""
                    for arg in value.args:
                        if isinstance(arg, ast.Call) and _call_name(arg) == "ForeignKey":
                            continue
                        type_expr = ast.unparse(arg)
                        break
                    columns.append(
                        ColumnSpec(
                            name=target_name,
                            type_expr=type_expr,
                            primary_key=_kw_bool(value, "primary_key", False),
                            nullable=_kw_bool(value, "nullable", True),
                            foreign_key=_find_fk(value),
                            ondelete=_find_ondelete(value),
                            source=ast.get_source_segment(source, stmt) or "",
                        )
                    )
                    continue
                # ---- Django ORM fields ----
                if cname in DJANGO_FIELD_CALLS:
                    # ForeignKey / OneToOneField: first arg is the related model string or class
                    fk_target: str | None = None
                    nullable = True
                    primary_key = False
                    if cname in ("ForeignKey", "OneToOneField"):
                        if value.args and isinstance(value.args[0], ast.Constant) and isinstance(value.args[0].value, str):
                            fk_target = value.args[0].value
                        for kw in value.keywords:
                            if kw.arg == "null" and isinstance(kw.value, ast.Constant):
                                nullable = bool(kw.value.value)
                    elif cname in ("AutoField", "BigAutoField", "SmallAutoField"):
                        primary_key = True
                        nullable = False
                    for kw in value.keywords:
                        if kw.arg == "primary_key" and isinstance(kw.value, ast.Constant):
                            primary_key = bool(kw.value.value)
                    columns.append(
                        ColumnSpec(
                            name=target_name,
                            type_expr=cname,
                            primary_key=primary_key,
                            nullable=nullable,
                            foreign_key=fk_target,
                            ondelete=None,
                            source=ast.get_source_segment(source, stmt) or "",
                        )
                    )

            entities.append(
                EntityModel(
                    class_name=cls.name,
                    table=gnode.table or cls.name.lower() + "s",
                    module=module,
                    symbol=gnode.id,
                    lineno=cls.lineno,
                    end_lineno=cls.end_lineno or cls.lineno,
                    columns=columns,
                    relationships=relationships,
                )
            )
    return entities


def sever_column_source(source: str, uuid_refs: bool) -> str:
    """Rewrite one `x = Column(Integer, ForeignKey('t.c'), ...)` line into a soft reference."""
    out = _FK_RE.sub("", source, count=1)
    if uuid_refs:
        out = re.sub(r"\((?:db\.)?(Integer|BigInteger|SmallInteger)\b", r"(String(36)", out, count=1)
    out = re.sub(r"\(\s*,\s*", "(", out)
    out = re.sub(r",\s*,", ",", out)
    if "index=" not in out:
        out = out.rstrip()
        if out.endswith(")"):
            out = out[:-1].rstrip().rstrip(",") + ", index=True)"
    return out
