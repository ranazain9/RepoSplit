"""Auto-healing: diagnose failed parity cases and patch the ported service source.

Two layers:
  1. Deterministic heuristics for the regressions the porter itself predicts (its porting notes).
  2. An LLM decision (HealDecision) for everything else, with the legacy source as ground truth.
Patches are exact find/replace edits so they are reviewable and reversible.
"""

from __future__ import annotations

import re
from pathlib import Path

from reposplit.core.schemas import HealDecision, HealPatch, ParityCase, ScaffoldManifest
from reposplit.utils.naming import to_snake


def _route_function(case: ParityCase) -> str:
    return case.id.split("_", 1)[1].replace("_not_found", "").replace("_after_writes", "").replace("_after_cancel", "").replace("_idempotent", "")


def _model_owners(output_dir: Path) -> dict[str, str]:
    owners: dict[str, str] = {}
    services_dir = output_dir / "services"
    if not services_dir.exists():
        return owners
    for svc_dir in services_dir.iterdir():
        if not svc_dir.is_dir():
            continue
        models_py = svc_dir / "app" / "models.py"
        if models_py.exists():
            content = models_py.read_text(encoding="utf-8", errors="ignore")
            for m in re.finditer(r"^class\s+(\w+)\s*\(", content, flags=re.MULTILINE):
                owners[m.group(1)] = svc_dir.name
    return owners


def _decompose_generic_join(
    source: str, service: str, output_dir: Path
) -> tuple[str, str] | None:
    multi_query_re = re.compile(
        r"(?P<lhs>\w+)\s*=\s*\(\s*session\.query\((?P<models>[^)]+)\)"
        r"(?P<chain>[\s\S]*?)\.all\(\)\s*\)",
        re.MULTILINE,
    )
    m = multi_query_re.search(source)
    if not m:
        return None
    raw_models = [s.strip() for s in m.group("models").split(",")]
    if len(raw_models) <= 1:
        return None

    model_owners = _model_owners(output_dir)
    foreign_in_query = [(mdl, model_owners[mdl]) for mdl in raw_models if mdl in model_owners and model_owners[mdl] != service]
    local_models = [mdl for mdl in raw_models if model_owners.get(mdl) == service or mdl not in model_owners]
    if not foreign_in_query or not local_models:
        return None

    lhs = m.group("lhs")
    chain = m.group("chain")
    foreign_names = {f for f, _ in foreign_in_query}

    join_clauses = re.findall(r"\.join\([^)]+\)", chain)
    local_joins = [jc for jc in join_clauses if not any(f in jc for f in foreign_names)]
    filter_clauses = re.findall(r"\.filter\([^)]+\)", chain)
    order_clauses = re.findall(r"\.order_by\([^)]+\)", chain)

    local_models_str = ", ".join(local_models)
    local_query_var = f"_{lhs}_local"

    lines = [
        f"{local_query_var} = (",
        f"        session.query({local_models_str})",
    ]
    for jc in local_joins:
        lines.append(f"        {jc}")
    for fc in filter_clauses:
        lines.append(f"        {fc}")
    for oc in order_clauses:
        lines.append(f"        {oc}")
    lines.append("        .all()\n    )")

    lines.append(f"{lhs} = []")
    if len(local_models) == 1:
        loop_pattern = f"_{to_snake(local_models[0])}"
    else:
        loop_vars = [f"_{to_snake(lm)}" for lm in local_models]
        loop_pattern = f"{', '.join(loop_vars)}"

    lines.append(f"for {loop_pattern} in {local_query_var}:")
    var_map: dict[str, str] = {lm: f"_{to_snake(lm)}" for lm in local_models}

    for fmdl, fowner in foreign_in_query:
        f_var = f"_{to_snake(fmdl)}"
        var_map[fmdl] = f_var
        fk_col = f"{to_snake(fmdl)}_id"
        op = f"get_{to_snake(fmdl)}"
        fk_lookups = [f"getattr({var_map[lm]}, '{fk_col}', None)" for lm in local_models]
        fk_expr = fk_lookups[0] if len(fk_lookups) == 1 else " or ".join(fk_lookups)
        lines.append(f"    _fk_{to_snake(fmdl)} = {fk_expr}")
        lines.append(
            f"    {f_var} = clients.{fowner}.{op}(_fk_{to_snake(fmdl)}) if _fk_{to_snake(fmdl)} is not None else None"
        )

    tuple_items = [var_map[m_name] for m_name in raw_models]
    lines.append(f"    {lhs}.append(({', '.join(tuple_items)}))")

    old_code = m.group(0)
    new_code = "\n    ".join(lines)
    return old_code, new_code


def heuristic_decision(cases: list[ParityCase], manifest: ScaffoldManifest, output_dir: Path) -> HealDecision:
    """Fast path: map failures onto the porter's own notes; propose safe mechanical patches.

    Two detection layers:
    1. If the porter already rewrote the cross-service JOIN (note 'cross_service_join_rewritten'),
       skip this case — no heap patch needed; it was resolved at port time.
    2. If the old verbatim JOIN string is still in the source (porter regex didn't fire due to
       whitespace differences), apply the known-good replacement as a heal patch.
    """
    decision = HealDecision(rationale="heuristic auto-heal pass")
    for case in cases:
        fn = _route_function(case)
        svc = manifest.services.get(case.service)
        all_notes = svc.porting_notes if svc else []
        notes = [n for n in all_notes if n.startswith(f"{fn}:")]
        main_py = output_dir / "services" / case.service / "app" / "main.py"
        source = main_py.read_text(encoding="utf-8") if main_py.exists() else ""

        # Skip: porter already rewrote this cross-service JOIN at scaffold time
        if any("cross_service_join_rewritten" in n for n in all_notes):
            decision.needs_human.append(
                f"{case.id}: cross-service JOIN was rewritten at port time but parity still fails "
                f"— check clients.*.get_*() implementation in services/{case.service}/app/clients.py"
            )
            continue

        # Fallback: old verbatim JOIN string still present (porter regex didn't fire)
        if fn == "order_history" and "session.query(Order, OrderItem, Product)" in source:
            old_str = (
                "rows = (\n"
                "        session.query(Order, OrderItem, Product)\n"
                "        .join(OrderItem, OrderItem.order_id == Order.id)\n"
                "        .join(Product, Product.id == OrderItem.product_id)\n"
                "        .filter(Order.user_id == user_id)\n"
                "        .order_by(Order.id, OrderItem.id)\n"
                "        .all()\n"
                "    )\n"
                "    history = {}\n"
                "    for order, item, product in rows:\n"
                "        entry = history.setdefault(\n"
                '            order.id, {"order_id": order.id, "status": order.status, "total": order.total, "lines": []}\n'
                "        )\n"
                "        entry[\"lines\"].append(\n"
                '            {"product": product.name, "sku": product.sku, "quantity": item.quantity, "unit_price": item.unit_price}\n'
                "        )"
            )
            new_str = (
                "orders = (\n"
                "        session.query(Order)\n"
                "        .filter(Order.user_id == user_id)\n"
                "        .order_by(Order.id)\n"
                "        .all()\n"
                "    )\n"
                "    history = {}\n"
                "    for order in orders:\n"
                "        entry = history.setdefault(\n"
                '            order.id, {"order_id": order.id, "status": order.status, "total": order.total, "lines": []}\n'
                "        )\n"
                "        for item in sorted(order.items, key=lambda i: i.id):\n"
                "            product = clients.catalog_service.get_product(item.product_id)\n"
                "            entry[\"lines\"].append(\n"
                '                {"product": product.name if product else None, "sku": product.sku if product else None, "quantity": item.quantity, "unit_price": item.unit_price}\n'
                "            )"
            )
            if old_str in source:
                decision.patches.append(
                    HealPatch(
                        case_id=case.id,
                        file=f"services/{case.service}/app/main.py",
                        find=old_str,
                        replace=new_str,
                        rationale="resolve cross-service JOIN in order_history via catalog_service client",
                        confidence=1.0,
                    )
                )
                continue

        # Generalized cross-service JOIN query decomposition:
        generic_join = _decompose_generic_join(source, case.service, output_dir)
        if generic_join:
            old_join, new_join = generic_join
            decision.patches.append(
                HealPatch(
                    case_id=case.id,
                    file=f"services/{case.service}/app/main.py",
                    find=old_join,
                    replace=new_join,
                    rationale=f"resolve cross-service JOIN in {fn} via client calls",
                    confidence=0.95,
                )
            )
            continue

        if any("foreign model" in n for n in notes):
            decision.needs_human.append(
                f"{case.id}: cross-service JOIN in {fn} - wire it to the CQRS projection generated in data/cqrs_views.py"
            )
            continue
        # 422 from FastAPI validation where the monolith returned 400: the ported handler read a missing key.
        if case.service_status == 422 and case.monolith_status == 400:
            decision.needs_human.append(f"{case.id}: request validation differs (422 vs 400) - align body schema")
            continue
        # Status-only mismatch on a POST that creates: monolith 201 vs service 200 means a bare `return x`.
        if case.monolith_status == 201 and case.service_status == 200 and not [d for d in case.diff if not d.startswith("status")]:
            m = re.search(rf"def {fn}\(.*?\n(?P<body>(?:    .*\n)+)", source)
            if m and "return " in m.group("body"):
                ret = m.group("body").rstrip().splitlines()[-1]
                expr = ret.strip()[len("return ") :]
                decision.patches.append(
                    HealPatch(
                        case_id=case.id,
                        file=f"services/{case.service}/app/main.py",
                        find=ret,
                        replace=ret.replace(f"return {expr}", f"return JSONResponse(content={expr}, status_code=201)"),
                        rationale="monolith returns 201 Created for this route",
                        confidence=0.9,
                    )
                )
                continue
        if any(d.startswith("status 5") is False and "!=" in d for d in case.diff) and any("!= 500" in d for d in case.diff):
            decision.needs_human.append(f"{case.id}: service raised an unhandled error - see services/{case.service} logs")
    return decision


def apply_patches(patches: list[HealPatch], output_dir: Path, manifest: ScaffoldManifest) -> list[HealPatch]:
    applied: list[HealPatch] = []
    for patch in patches:
        target = output_dir / patch.file
        if not target.exists():
            continue
        text = target.read_text(encoding="utf-8")
        if patch.find not in text:
            continue
        target.write_text(text.replace(patch.find, patch.replace, 1), encoding="utf-8")
        svc = patch.file.split("/")[1] if patch.file.startswith("services/") else None
        if svc and svc in manifest.services:
            marker = f"healed:{patch.file}"
            if marker not in manifest.services[svc].porting_notes:
                manifest.services[svc].porting_notes.append(marker)
        applied.append(patch)
    return applied


def heal_context(cases: list[ParityCase], output_dir: Path) -> str:
    """Compact prompt context for the LLM: request, both responses, ported + legacy source per failing case."""
    blocks: list[str] = []
    for case in cases[:8]:
        fn = _route_function(case)
        svc_dir = output_dir / "services" / case.service / "app"
        ported = _extract_function(svc_dir / "main.py", fn)
        legacy = _extract_function(svc_dir / "legacy_reference.py", fn)
        blocks.append(
            "\n".join(
                [
                    f"### case {case.id} ({case.method} {case.path}) service={case.service} file=services/{case.service}/app/main.py",
                    f"request payload: {case.payload}",
                    f"monolith: {case.monolith_status} {str(case.monolith_body)[:600]}",
                    f"service:  {case.service_status} {str(case.service_body)[:600]}",
                    "diff: " + "; ".join(case.diff[:10]),
                    "--- ported ---",
                    ported,
                    "--- legacy ---",
                    legacy,
                ]
            )
        )
    return "\n\n".join(blocks)


def _extract_function(path: Path, name: str) -> str:
    if not path.exists():
        return "(missing)"
    text = path.read_text(encoding="utf-8")
    m = re.search(rf"(^@.*\n)*^def {re.escape(name)}\(.*?(?=^\S)", text, flags=re.S | re.M)
    return m.group(0).rstrip() if m else "(not found)"
