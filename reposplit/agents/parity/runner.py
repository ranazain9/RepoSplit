"""Differential runner: dispatch each case to the monolith and to the new services, then diff."""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from reposplit.agents.parity.semantic_diff import diff, normalize
from reposplit.core.schemas import ContractPlan, ParityCase


@dataclass
class Targets:
    monolith_url: str
    gateway_url: str | None = None
    service_urls: dict[str, str] | None = None

    def base_for(self, service: str) -> str:
        if self.service_urls and service in self.service_urls:
            return self.service_urls[service]
        if self.gateway_url:
            return self.gateway_url
        raise KeyError(f"no target URL for service {service}")


class DifferentialRunner:
    def __init__(self, targets: Targets, masks: list[str], timeout: float = 15.0) -> None:
        self.targets = targets
        self.masks = masks
        self._http = httpx.Client(timeout=timeout)

    def _send(self, base: str, case: ParityCase) -> tuple[int, object, float]:
        t0 = time.perf_counter()
        is_get_or_head = case.method.upper() in ("GET", "HEAD")
        json_body = None if is_get_or_head else case.payload
        params = case.payload if is_get_or_head and case.payload else None
        resp = self._http.request(
            case.method,
            base.rstrip("/") + case.path,
            json=json_body,
            params=params,
            headers=case.headers,
        )
        elapsed = (time.perf_counter() - t0) * 1000
        try:
            body = resp.json()
        except ValueError:
            body = resp.text
        return resp.status_code, body, round(elapsed, 2)

    def run_case(self, case: ParityCase) -> ParityCase:
        try:
            m_status, m_body, m_ms = self._send(self.targets.monolith_url, case)
            s_status, s_body, s_ms = self._send(self.targets.base_for(case.service), case)
        except (httpx.TransportError, KeyError) as exc:
            case.status = "ERROR"
            case.diff = [f"transport error: {exc}"]
            return case
        case.monolith_status, case.service_status = m_status, s_status
        case.monolith_body, case.service_body = m_body, s_body
        case.latency_ms = {"monolith": m_ms, "service": s_ms}
        problems: list[str] = []
        if m_status != s_status:
            problems.append(f"status {m_status} != {s_status}")
        problems += diff(normalize(m_body, self.masks), normalize(s_body, self.masks))
        case.diff = problems
        case.status = "PASS" if not problems else "FAIL"
        return case

    def run(self, cases: list[ParityCase], on_case: Callable[[ParityCase], None] | None = None) -> list[ParityCase]:
        out: list[ParityCase] = []
        for case in cases:
            result = self.run_case(case)
            if on_case:
                on_case(result)
            out.append(result)
        return out

    def close(self) -> None:
        self._http.close()


def service_for_path(contracts: ContractPlan, method: str, path: str) -> str | None:
    for c in contracts.services.values():
        for ep in c.endpoints:
            pattern = "^" + re.sub(r"\{\w+\}", r"[^/]+", ep.path) + "$"
            if ep.exposure == "public" and ep.method == method and re.match(pattern, path):
                return c.service
    return None
