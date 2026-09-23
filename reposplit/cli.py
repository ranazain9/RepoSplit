"""RepoSplit 2.0 command line.

    reposplit run examples/shop_monolith --out out --provider mock --yes
    reposplit run examples/shop_monolith --live          # boots monolith + services, runs live parity
    reposplit graph examples/shop_monolith               # topology only, no artifacts
    reposplit parity --suite out/reports/parity_suite.json --monolith-url ... --services-url ...
    reposplit verify out/reports/migration_passport.json
    reposplit serve                                      # dashboard + SSE telemetry API
    reposplit agents
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

from reposplit import __version__
from reposplit.core.schemas import Phase, RunConfig

app = typer.Typer(help="RepoSplit 2.0 - autonomous monolith-to-microservice modernization engine", no_args_is_help=True)
console = Console(emoji=False)


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(console=console, show_path=False, rich_tracebacks=True)],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


@app.callback()
def _root(version: bool = typer.Option(False, "--version", help="print version and exit")) -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    if version:
        console.print(f"reposplit {__version__}")
        raise typer.Exit()


@app.command()
def run(
    repo: Path = typer.Argument(..., exists=True, file_okay=False, help="path to the monolith repository"),
    out: Path = typer.Option(Path("out"), "--out", "-o", help="output directory for generated artifacts"),
    provider: str = typer.Option("auto", help="auto | mock | anthropic | watsonx | groq"),
    model: str | None = typer.Option(None, help="model id override (e.g. claude-opus-5)"),
    mode: str = typer.Option("full", help="full | strangler"),
    service: list[str] = typer.Option([], "--service", "-s", help="strangler mode: cluster(s) to extract"),
    canary: int = typer.Option(10, min=0, max=100, help="initial canary weight per route (%)"),
    live: bool = typer.Option(False, "--live", help="boot monolith + generated services locally and run live parity"),
    monolith_url: str | None = typer.Option(None, help="running monolith base URL for live parity"),
    services_url: str | None = typer.Option(None, help="running gateway base URL for live parity"),
    heal: bool = typer.Option(True, "--heal/--no-heal", help="auto-healing loop on parity regressions"),
    max_heal: int = typer.Option(3, help="max auto-heal iterations"),
    strict_parity: bool = typer.Option(False, help="fail the run if parity regressions remain after healing"),
    yes: bool = typer.Option(False, "--yes", "-y", help="skip the human approval gate before scaffolding"),
    strict_llm: bool = typer.Option(False, help="fail the run instead of falling back to deterministic defaults"),
    uuid_refs: bool = typer.Option(False, help="sever FKs to String(36) UUID soft references"),
    signing_key: Path | None = typer.Option(None, help="Ed25519 PEM for the Migration Passport (ephemeral if omitted)"),
    topology_override: Path | None = typer.Option(None, "--topology-override", help="path to JSON file with manual symbol/file -> cluster overrides"),
    seed: int = typer.Option(42, help="deterministic seed for Louvain / payload synthesis"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
    resume: bool = typer.Option(False, "--resume", "-r", help="resume from the last saved blackboard snapshot in --out"),
    gateway: str = typer.Option("envoy", help="gateway config to generate: envoy | kong | both"),
) -> None:
    """Run the full multi-agent modernization pipeline."""
    _setup_logging(verbose)
    config = RunConfig(
        repo_path=str(repo),
        output_dir=str(out),
        provider=provider,  # type: ignore[arg-type]
        model=model,
        mode=mode,  # type: ignore[arg-type]
        target_services=service,
        canary_weight=canary,
        live=live,
        monolith_url=monolith_url,
        services_url=services_url,
        auto_heal=heal,
        max_heal_iterations=max_heal,
        strict_parity=strict_parity,
        require_approval=not yes,
        strict_llm=strict_llm,
        uuid_refs=uuid_refs,
        signing_key_path=str(signing_key) if signing_key else None,
        topology_override=str(topology_override) if topology_override else None,
        seed=seed,
        gateway=gateway,  # type: ignore[arg-type]
    )
    from reposplit.core.supervisor import Supervisor

    if resume:
        snapshot = out / ".reposplit" / "blackboard.json"
        if not snapshot.exists():
            console.print(f"[red]No snapshot found at {snapshot}. Run without --resume first.[/]")
            raise typer.Exit(code=1)
        console.print(f"[bold cyan]Resuming run from snapshot:[/] {snapshot}")
        supervisor = Supervisor.from_snapshot(snapshot, config)
    else:
        supervisor = Supervisor(config)
    if config.require_approval:
        # CLI approval: ask once the STRANGLER phase is done. Interactive terminals only.
        async def _cli_approval() -> None:
            while supervisor.phase not in (Phase.STRANGLER, Phase.COMPLETE, Phase.FAILED):
                await asyncio.sleep(0.2)
            while supervisor.phase == Phase.STRANGLER and not supervisor.ctx.approval.is_set():
                await asyncio.sleep(0.2)
                if supervisor.bb.events() and supervisor.bb.events()[-1].payload.get("approval_required"):
                    console.print("\n[bold yellow]Human oversight gate:[/] topology, data plan and contracts are on disk under "
                                  f"[cyan]{out}/reports[/]. Scaffold the services?")
                    if typer.confirm("Approve", default=True):
                        supervisor.approve()
                    else:
                        supervisor.fsm.fail("rejected by operator")
                        raise typer.Exit(code=2)

        async def _main():
            await asyncio.gather(supervisor.run(), _cli_approval())
            return supervisor.summary

        summary = asyncio.run(_main())
    else:
        summary = asyncio.run(supervisor.run())

    _print_summary(summary, out)
    raise typer.Exit(code=0 if summary.status == Phase.COMPLETE else 1)


def _print_summary(summary, out: Path) -> None:
    table = Table(title=f"RepoSplit run {summary.run_id} - {summary.status}", show_lines=False)
    table.add_column("Phase", style="cyan")
    table.add_column("Agent")
    table.add_column("OK")
    table.add_column("Summary")
    table.add_column("s", justify="right")
    for r in summary.results:
        table.add_row(r.phase, r.agent, "[green]yes" if r.ok else "[red]no", r.summary, f"{r.duration_s:.2f}")
    console.print(table)
    if summary.error:
        console.print(f"[red]error:[/] {summary.error}")
    console.print(f"artifacts: {len(summary.artifacts)} file(s) under [cyan]{out}[/]")


@app.command()
def graph(
    repo: Path = typer.Argument(..., exists=True, file_okay=False),
    seed: int = typer.Option(42),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Parse the repository and print the proposed domain topology (no artifacts written)."""
    from reposplit.agents.architect.ast_parser import PythonRepoParser
    from reposplit.agents.architect.graph_metrics import coupling, partition_symbols, symbol_graph

    g = PythonRepoParser(repo).parse()
    part = partition_symbols(g, seed=seed)
    sg = symbol_graph(g)
    if as_json:
        console.print_json(json.dumps({k: v for k, v in part.members.items()}))
        return
    table = Table(title=f"{repo} - {g.file_count} modules, {len(g.nodes)} symbols, modularity {part.modularity}")
    table.add_column("Cluster", style="cyan")
    table.add_column("Ca", justify="right")
    table.add_column("Ce", justify="right")
    table.add_column("I", justify="right")
    table.add_column("Symbols")
    for name, syms in part.members.items():
        m = coupling(sg, set(syms))
        table.add_row(name, str(m.ca), str(m.ce), f"{m.instability:.2f}", ", ".join(s.split("::")[-1] for s in syms))
    console.print(table)


@app.command()
def parity(
    suite: Path = typer.Option(Path("out/reports/parity_suite.json"), exists=True),
    monolith_url: str = typer.Option(..., help="legacy monolith base URL"),
    services_url: str = typer.Option(..., help="gateway (or single service) base URL"),
    out: Path = typer.Option(Path("out/reports/parity_report.live.json")),
) -> None:
    """Run the differential parity suite against two running targets."""
    from reposplit.agents.parity.runner import DifferentialRunner, Targets
    from reposplit.agents.parity.semantic_diff import DEFAULT_MASKS
    from reposplit.core.schemas import ParityCase

    cases = [ParityCase.model_validate(c) for c in json.loads(suite.read_text(encoding="utf-8"))]
    runner = DifferentialRunner(Targets(monolith_url=monolith_url, gateway_url=services_url), DEFAULT_MASKS)
    results = runner.run(cases, lambda c: console.print(f"[{'green' if c.status == 'PASS' else 'red'}]{c.status:5}[/] {c.id} {c.method} {c.path} {'; '.join(c.diff[:2])}"))
    passed = sum(1 for c in results if c.status == "PASS")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps([c.model_dump(mode="json") for c in results], indent=2), encoding="utf-8")
    console.print(f"parity: {passed}/{len(results)} ({passed / len(results):.0%}) -> {out}")
    raise typer.Exit(code=0 if passed == len(results) else 1)


@app.command()
def verify(
    passport: Path = typer.Argument(Path("out/reports/migration_passport.json"), exists=True),
    check_artifacts: bool = typer.Option(True, help="re-hash subjects under the passport's output dir"),
    key: Path | None = typer.Option(None, "--key", "-k", help="trusted Ed25519 public key (.pub or .pem)"),
    did: str | None = typer.Option(None, "--did", help="expected signer did:key string"),
    artifacts_dir: Path | None = typer.Option(None, "--artifacts-dir", "-d", help="base output dir for subject artifacts"),
) -> None:
    """Verify a Migration Passport's DSSE signature and artifact digests."""
    from reposplit.agents.governance.attestation import load_passport, verify_passport, verify_subjects

    p = load_passport(passport)
    ok, problems = verify_passport(p, trusted_key=key, trusted_did=did)
    pred = p.statement.predicate
    console.print(f"signer   : {p.signer_did}")
    console.print(f"engine   : {pred.get('modernizationEngine')} v{pred.get('engineVersion')} run {pred.get('runId')}")
    console.print(f"source   : tree sha256 {pred.get('source', {}).get('treeSha256')}")
    console.print(f"parity   : {pred.get('parityTestVerification', {}).get('passRate')} ({pred.get('parityTestVerification', {}).get('mode')})")
    console.print(f"prompts  : {len(pred.get('modelProvenance', {}).get('prompts', []))} hashed, {pred.get('modelProvenance', {}).get('llmDecisions')} by LLM")
    if "finOpsRoi" in pred:
        fo = pred["finOpsRoi"]
        console.print(f"finops   : ${fo.get('annualSavingsUsd', 0):,.0f}/yr savings ({fo.get('savingsPercent')}), {fo.get('roiMultiple')} ROI, -{fo.get('carbonReductionKgYr', 0):.0f}kg CO2e")
    base_dir = artifacts_dir or passport.parent.parent
    drift = verify_subjects(p, base_dir) if check_artifacts else []
    if ok and not drift:
        console.print("[bold green]signature valid, artifacts match attestation[/]")
        raise typer.Exit(code=0)
    for item in problems + drift:
        console.print(f"[red]x[/] {item}")
    raise typer.Exit(code=1)


@app.command()
def serve(host: str = typer.Option("127.0.0.1"), port: int = typer.Option(8765)) -> None:
    """Start the telemetry API + dashboard (SSE event stream, D3 untangling graph)."""
    import uvicorn

    from reposplit.api.server import create_app

    console.print(f"dashboard: http://{host}:{port}/")
    uvicorn.run(create_app(), host=host, port=port, log_level="info")


@app.command()
def agents() -> None:
    """List the agents in the execution DAG."""
    from reposplit.core.registry import describe_agents

    table = Table(title="RepoSplit 2.0 agent DAG")
    for col in ("phase", "name", "llm", "requires", "produces", "description"):
        table.add_column(col)
    for row in describe_agents():
        table.add_row(*[row[c] for c in ("phase", "name", "llm", "requires", "produces", "description")])
    console.print(table)


if __name__ == "__main__":
    app()
