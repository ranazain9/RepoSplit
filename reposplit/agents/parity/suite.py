"""Synthesizes an ordered differential test suite from the public endpoint contracts.

Ordering matters because both sides are stateful: create users before logging in, create an
order before fetching it, cancel last. The phases below encode that as a deterministic heuristic;
recorded production traffic (`--traffic file.jsonl`) can replace synthesis entirely.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from reposplit.agents.contract.typing_rules import sample_value
from reposplit.core.schemas import ContractPlan, EndpointContract, ParityCase

_ID_KEY = re.compile(r"(^|_)id$")
_DESTRUCTIVE = ("cancel", "delete", "remove", "refund", "release")

SAMPLE_OVERRIDES: dict[str, Any] = {"email": "carol@example.com", "password": "correct-horse-battery"}


def _rank(ep: EndpointContract) -> int:
    """Within a phase: creators before authenticators (register before login)."""
    name = ep.operation_id
    if any(v in name for v in ("register", "signup", "sign_up", "create")):
        return 0
    if any(v in name for v in ("login", "auth", "token")):
        return 1
    return 2


def _phase(ep: EndpointContract) -> int:
    name = ep.operation_id
    keys = ep.body_keys or [p.name for p in ep.params]
    # tenant_id is request context, not an entity reference
    has_ref = bool(ep.path_params) or any(_ID_KEY.search(k) for k in keys if k != "tenant_id")
    if ep.method == "GET":
        return 1 if not ep.path_params else 2
    if any(v in name for v in _DESTRUCTIVE):
        return 5
    if ep.method == "POST" and not has_ref:
        return 0  # register / login style
    return 3  # create with references (orders)


def _payload(ep: EndpointContract, seed: int) -> dict[str, Any] | None:
    if ep.method == "GET":
        return None
    hints = {p.name: p.type_hint for p in ep.params}
    keys = ep.body_keys or [p.name for p in ep.params if p.name not in ep.path_params]
    payload = {k: SAMPLE_OVERRIDES.get(k, sample_value(k, hints.get(k), seed)) for k in keys}
    return payload


def _path(ep: EndpointContract, ids: dict[str, int]) -> str:
    path = ep.path
    for p in ep.path_params:
        path = path.replace("{" + p + "}", str(ids.get(p, 1)))
    return path


def build_suite(contracts: ContractPlan, seed: int = 1) -> list[ParityCase]:
    """`seed` is the sample entity id used for references (seeded rows start at 1)."""
    endpoints = [ep for c in contracts.services.values() for ep in c.endpoints if ep.exposure == "public"]
    endpoints.sort(key=lambda e: (_phase(e), _rank(e), e.path, e.method))
    cases: list[ParityCase] = []
    headers = {"X-User-Id": "1", "X-Tenant-Id": "acme"}

    def add(ep: EndpointContract, suffix: str, path: str, payload: dict[str, Any] | None) -> None:
        cases.append(
            ParityCase(
                id=f"{len(cases) + 1:03d}_{ep.operation_id}{suffix}",
                service=ep.service,
                method=ep.method,
                path=path,
                payload=payload,
                headers=headers,
            )
        )

    for ep in endpoints:
        phase = _phase(ep)
        if phase == 5:
            continue
        add(ep, "", _path(ep, {}), _payload(ep, seed))
        if phase == 2:
            add(ep, "_not_found", _path(ep, dict.fromkeys(ep.path_params, 999_999)), None)
    # Observe created state, then destructive actions, then observe again.
    for ep in endpoints:
        if _phase(ep) == 2:
            add(ep, "_after_writes", _path(ep, {}), None)
    for ep in endpoints:
        if _phase(ep) == 5:
            add(ep, "", _path(ep, {}), _payload(ep, seed))
            add(ep, "_idempotent", _path(ep, {}), _payload(ep, seed))
    for ep in endpoints:
        if _phase(ep) == 2:
            add(ep, "_after_cancel", _path(ep, {}), None)
    return cases


def load_traffic(path: Path, contracts: ContractPlan) -> list[ParityCase]:
    """Replay recorded traffic: one JSON object per line {method, path, payload?, headers?}."""
    route_owner: list[tuple[re.Pattern[str], str, str]] = []
    for c in contracts.services.values():
        for ep in c.endpoints:
            if ep.exposure == "public":
                regex = "^" + re.sub(r"\{\w+\}", r"[^/]+", ep.path) + "$"
                route_owner.append((re.compile(regex), ep.method, ep.service))
    cases: list[ParityCase] = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        rec = json.loads(line)
        svc = next((s for rx, m, s in route_owner if m == rec["method"].upper() and rx.match(rec["path"])), "unknown")
        cases.append(
            ParityCase(
                id=f"{i:03d}_replay",
                service=svc,
                method=rec["method"].upper(),
                path=rec["path"],
                payload=rec.get("payload"),
                headers=rec.get("headers", {}),
            )
        )
    return cases
