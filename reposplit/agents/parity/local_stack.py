"""Boot the monolith and the generated services as local subprocesses (no Docker required).

Used by `reposplit run --live`, `reposplit demo` and the integration test. Each process gets a
fresh SQLite file so both sides start from identical seed data.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

import httpx

from reposplit.core.schemas import ContractPlan


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_healthy(url: str, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            r = httpx.get(url + "/health", timeout=2.0)
            if r.status_code == 200:
                return
            last = f"{r.status_code}"
        except httpx.TransportError as exc:
            last = str(exc)
        time.sleep(0.3)
    raise RuntimeError(f"{url} did not become healthy: {last}")


@contextmanager
def local_stack(repo_root: Path, output_dir: Path, contracts: ContractPlan, log_dir: Path | None = None):
    """Yields (monolith_url, {service: url}). Tears everything down on exit."""
    procs: list[subprocess.Popen] = []
    tmp = Path(tempfile.mkdtemp(prefix="reposplit-stack-"))
    log_dir = log_dir or tmp
    log_dir.mkdir(parents=True, exist_ok=True)
    ports = {svc: free_port() for svc in contracts.services}
    service_urls = {svc: f"http://127.0.0.1:{p}" for svc, p in ports.items()}
    monolith_port = free_port()
    monolith_url = f"http://127.0.0.1:{monolith_port}"
    base_env = {k: v for k, v in os.environ.items() if not k.endswith("_URL")}
    base_env["PYTHONUNBUFFERED"] = "1"

    def spawn(name: str, cmd: list[str], cwd: Path, env: dict[str, str]) -> None:
        log = (log_dir / f"{name}.log").open("w", encoding="utf-8")
        procs.append(subprocess.Popen(cmd, cwd=str(cwd), env=env, stdout=log, stderr=subprocess.STDOUT))

    try:
        spawn(
            "monolith",
            [sys.executable, "app.py"],
            repo_root,
            {**base_env, "PORT": str(monolith_port), "DATABASE_URL": f"sqlite:///{(tmp / 'monolith.sqlite').as_posix()}"},
        )
        for svc, contract in contracts.services.items():
            env = {
                **base_env,
                "PORT": str(ports[svc]),
                "DATABASE_URL": f"sqlite:///{(tmp / f'{svc}.sqlite').as_posix()}",
                **{f"{dep.upper()}_URL": service_urls[dep] for dep in contract.downstream},
            }
            spawn(
                svc,
                [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(ports[svc]), "--log-level", "warning"],
                output_dir / "services" / svc,
                env,
            )
        _wait_healthy(monolith_url)
        for svc, url in service_urls.items():
            try:
                _wait_healthy(url)
            except RuntimeError as exc:
                log_text = (log_dir / f"{svc}.log").read_text(encoding="utf-8", errors="ignore")[-1500:]
                raise RuntimeError(f"{exc}\n--- {svc}.log ---\n{log_text}") from exc
        yield monolith_url, service_urls
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
