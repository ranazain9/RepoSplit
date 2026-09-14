# Instructions for coding agents (IBM Bob, Claude Code, Cursor, Codex)

You are working in **RepoSplit 2.0**, a Python 3.11+ multi-agent engine that splits monoliths into
microservices. Read `CONTRIBUTING.md` for the agent contract and `docs/ARCHITECTURE.md` for the
design. Keep these invariants:

- Run `python -m ruff check reposplit tests examples` and `python -m pytest` before finishing.
  The live integration test spawns subprocesses; set `REPOSPLIT_SKIP_LIVE=1` if ports are unavailable.
- Deterministic tooling is the source of truth; LLM calls go through `BaseAgent.decide()` with a
  default and a Pydantic schema. Never add a raw `requests`/`httpx` call to an LLM API.
- All cross-agent state is a Pydantic model in `reposplit/core/schemas.py` stored on the Blackboard
  under a key from `Keys`. Do not pass dicts between agents.
- Generated code goes through `reposplit/generators/templates/*.j2` and `write_artifact()` so the
  Governance agent can hash it. Do not write files outside the run's `output_dir`.
- `examples/shop_monolith` is the benchmark: `reposplit run examples/shop_monolith --provider mock --yes --live`
  must stay green (≥ 80% parity; the only known failing cases are `order_history` until the CQRS
  wiring lands).
- Prefer small, reviewable diffs; leave `TODO(team): ...` markers for consciously deferred work.
