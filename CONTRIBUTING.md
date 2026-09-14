# Contributing (team guide)

This is a hackathon scaffold: the DAG is complete and runs end to end, and the fastest way to
add value is to deepen one agent at a time. Everything below is designed so two people can work
in parallel without stepping on each other.

## Setup

```bash
python -m venv .venv && . .venv/Scripts/activate   # or source .venv/bin/activate
pip install -e ".[all]"
make test && make lint
reposplit run examples/shop_monolith --provider mock --yes --live
```

## Where things live

```
reposplit/
  core/        supervisor.py (FSM + gates + heal loop)  blackboard.py  base_agent.py  schemas.py  registry.py
  llm/         provider.py (factory)  mock_provider.py  anthropic_provider.py  watsonx_provider.py
  agents/
    architect/ ast_parser.py  graph_metrics.py  prompts.py  agent.py
    data/      schema_parser.py  prompts.py  agent.py
    contract/  typing_rules.py  generators.py (OpenAPI/proto)  agent.py
    strangler/ agent.py (Envoy config, canary plan)
    parity/    suite.py  runner.py  semantic_diff.py  local_stack.py  healer.py  prompts.py  agent.py
    governance/attestation.py (in-toto/DSSE/Ed25519)  agent.py
  generators/  porting.py (Flask->FastAPI)  scaffold_agent.py  render.py  templates/**
  api/         server.py (SSE telemetry API)  static/index.html (reference dashboard)
examples/shop_monolith/   the benchmark
tests/                    unit + live integration
```

## The agent contract (read this before touching an agent)

```python
class MyAgent(BaseAgent):
    name = "my_agent"
    phase = Phase.DATA                     # one of the FSM phases
    requires = (Keys.TOPOLOGY,)            # blackboard keys that must exist (preflight)
    produces = (Keys.DATA_PLAN,)           # keys you must put (postflight)
    uses_llm = True

    async def execute(self) -> AgentResult:
        topo = self.bb.require(Keys.TOPOLOGY, DomainTopology)   # typed read
        default = ...                                            # deterministic answer FIRST
        decision = await self.decide(system=..., user=..., schema=MySchema, default=default)
        self.bb.put(Keys.DATA_PLAN, plan)                        # typed write
        self.write_artifact("reports/x.json", plan)             # registered for the passport
        return self.ok("summary for the table", metric=1)
```

Rules that keep the engine trustworthy:

1. **Deterministic first, LLM second.** Compute the answer with real tooling, then let `decide()` refine it. The mock provider returns the default, so CI never needs a key.
2. **Only Pydantic models cross agent boundaries.** Add new models to `core/schemas.py`, keys to `Keys`.
3. **Never write outside `output_dir`.** Use `write_artifact()` so the Governance agent can hash it.
4. **Log through `self.log()` / `self.warn()`.** That is what the dashboard streams.
5. **Register new agents in `core/registry.py`** and, if they need a new phase, extend `core/fsm.py` (`PHASE_ORDER`, `TRANSITIONS`).

## Good next tasks (ordered by demo impact)

| Task | Where | Notes |
|---|---|---|
| Wire `order_history` to the CQRS projection so parity hits 100% | `generators/porting.py` (or an LLM heal patch) | the exact failing case the demo can "heal" live |
| Real LLM auto-heal loop with Claude | `agents/parity/healer.py`, `prompts.py` | `HealDecision` is already validated + applied; needs prompt tuning |
| React + D3/Cytoscape dashboard | new `dashboard/` | consume `/api/runs/{id}/events` (SSE) and `/graph`; `api/static/index.html` shows the shapes |
| FinOps card (monolith $ vs autoscaled pods $) | new agent or Governance predicate | pure arithmetic from cluster LOC/route counts; judges love it |
| Django ORM + Django URL patterns in the parser | `agents/architect/ast_parser.py`, `agents/data/schema_parser.py` | `MODEL_BASES`, `_route_from_decorators` are the hooks |
| Kong declarative config alongside Envoy | `agents/strangler/agent.py` | `GatewayPlan.gateway = "kong"` |
| xDS-driven canary weights instead of editing YAML | `generators/templates/canary_controller.py.j2` | |
| watsonx provider verification | `llm/watsonx_provider.py` | check endpoint version + response shape |
| Resume a failed run from the blackboard snapshot | `core/supervisor.py` | `Blackboard.load()` already exists |

## Tests

- `tests/test_core.py` — blackboard, FSM, agent contract
- `tests/test_architect.py` — parser + partition invariants on the benchmark
- `tests/test_agents.py` — one shared offline run, per-agent assertions
- `tests/test_live_integration.py` — boots 5 processes, live parity, signed passport (`REPOSPLIT_SKIP_LIVE=1` to skip)

Add a test next to the agent you change; the shared `pipeline_run` fixture makes that cheap.

## Style

`ruff` with `line-length = 110`; type hints everywhere; docstrings explain *why*, comments explain
non-obvious *how*. Mark anything you consciously leave for later with `TODO(team): ...`.
