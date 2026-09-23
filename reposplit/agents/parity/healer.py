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


def _route_function(case: ParityCase) -> str:
    return case.id.split("_", 1)[1].replace("_not_found", "").replace("_after_writes", "").replace("_after_cancel", "").replace("_idempotent", "")


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
