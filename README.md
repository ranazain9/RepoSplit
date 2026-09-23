# RepoSplit 2.0 — Multi-Agent Modernization Engine

> Hierarchical Orchestrated Multi-Agent System (HOMAS) that turns a Python monolith into
> verified, deployable microservices — AST partitioning, database splitting (Sagas + CQRS),
> typed contracts, strangler-fig gateway, differential parity tests and a cryptographically
> signed **Migration Passport**. Built for the IBM Bob 2.0 Hackathon (lablab.ai).

```
Developer ──► Supervisor Orchestrator ──► Blackboard (shared state + telemetry stream)
                    │
   1 Architect ── 2 DataSplit ── 3 Contract ── 4 Strangler ── [scaffold] ── 5 Parity ⟲ heal ── 6 Governance
   AST+Louvain     FK sever      OpenAPI/gRPC   Envoy canary   FastAPI svcs   diff+patch      in-toto DSSE
```

## ▶️ Demo — click here

**[Open the live demo → http://127.0.0.1:8765](http://127.0.0.1:8765)** (start it first with the two commands below)

```bash
pip install -e ".[all]"
reposplit serve
```

Then in the dashboard: **Run** → **Untangle** (graph splits into 4 services, severed edges in red) →
**Approve scaffolding** (human-oversight gate) → watch the live parity results stream in.
Tick **live parity** before **Run** to boot the monolith + generated services as real processes.

Terminal-only version (no browser): `reposplit run examples/shop_monolith --provider mock --yes --live`
followed by `reposplit verify out/reports/migration_passport.json`. Full 3-minute script: [docs/DEMO_SCRIPT.md](docs/DEMO_SCRIPT.md).

---

**Status:** working vertical slice, hackathon scaffold. Everything in the DAG runs end to end
against the bundled `examples/shop_monolith` benchmark, offline (mock LLM) or with Claude /
watsonx. Extension points are marked `TODO(team)` in the code. See [CONTRIBUTING.md](CONTRIBUTING.md).

## 60-second demo

```bash
python -m venv .venv && . .venv/Scripts/activate      # Windows: .venv\Scripts\activate
pip install -e ".[all]"

# Offline, deterministic (no API key needed). Boots the monolith + 4 generated services locally
# as subprocesses and runs the differential parity suite across real HTTP boundaries.
reposplit run examples/shop_monolith --out out --provider mock --yes --live

reposplit verify out/reports/migration_passport.json     # Ed25519 / DSSE signature + artifact digests
reposplit serve                                          # http://127.0.0.1:8765 - SSE telemetry + D3 untangling graph
```

What you get in `out/`:

| Path | Produced by | What it is |
|---|---|---|
| `reports/dependency_graph.json`, `reports/domain_topology.json` | Architect | symbol-level call/data graph, clusters with Ca/Ce/Instability, severed edges, cycles |
| `data/isolated_schemas/<service>/models.py` | DataSplit | one SQLAlchemy schema per service, cross-service FKs severed to soft references |
| `data/migrations/001_sever_foreign_keys.sql`, `data/saga_orchestrator.py`, `data/cqrs_views.py`, `data/outbox.py` | DataSplit | migration DDL, generated Saga definitions (with compensations), CQRS projections, transactional outbox |
| `contracts/<service>/openapi.yaml`, `service.proto` | Contract | OpenAPI 3.0 + gRPC contracts; `X-User-Id` / `X-Tenant-Id` / `traceparent` propagation |
| `gateway/envoy.yaml`, `gateway/canary_controller.py`, `reports/canary_migration_plan.md` | Strangler | weighted canary routes (monolith default), outlier ejection, staged promotion, auto-rollback |
| `services/<service>/` | Scaffold | runnable FastAPI service per cluster: ported routes, internal RPCs, typed clients, middleware, Dockerfile |
| `docker-compose.yml`, `helm/reposplit/` | Scaffold | monolith + gateway + services topology; OpenShift-ready Helm chart with Instana/OTel env |
| `reports/parity_suite.json`, `reports/parity_report.json` | Parity | ordered differential cases, per-case status/diff/latency, applied heal patches, `parity_needs_human.md` |
| `reports/migration_passport.json`, `keys/attestation_key.pub` | Governance | in-toto v1 Statement in a DSSE envelope, Ed25519 `did:key` signer |
| `.reposplit/blackboard.json`, `events.jsonl`, `run_summary.json` | Supervisor | resumable state snapshot + full telemetry log |

## What the benchmark run proves

`examples/shop_monolith` is a 400-line Flask + SQLAlchemy store with one God `models.py`
(users, products, orders, order_items, payments), a multi-domain checkout and a cross-domain JOIN.

```
ARCHITECT   4 service clusters, 12 severed edges, coupling 0.77 -> 0.27, app.py routed to shared kernel
DATA        4 schemas, 4 FKs severed, CreateOrderSaga [ReserveStock -> ChargePayment -> Finalize] w/ real compensations, OrderHistoryView projection
CONTRACT    17 endpoints (9 public, 8 internal RPCs), OpenAPI + proto per service
STRANGLER   9 canary routes at 10%, Envoy weighted_clusters + Kong declarative routes
SCAFFOLD    4 FastAPI services, Flask & Django idioms ported mechanically (request/jsonify/abort/db.session/Model.query/severed calls)
PARITY      22/22 (100%) live parity across 5 real processes; cross-service JOINs decomposed into co-located queries + client calls
GOVERNANCE  signed Migration Passport (Ed25519 did:key); FinOps report ($1,892/yr, 58% carbon savings); HTML certificate
```

The benchmark achieves **100% differential parity (22/22 test cases pass)** with 0 failures and 0 errors.
Multi-table cross-domain queries are automatically decomposed by `porting.py` into local queries coupled
with typed downstream client calls, ensuring zero regression across severed service boundaries.

## Architecture in one screen

- **Supervisor** (`reposplit/core/supervisor.py`) — explicit FSM (`fsm.py`), phase gates, human-approval checkpoint (EU AI Act Art. 14), `--resume` from snapshots (`.reposplit/blackboard.json`), auto-heal loop `PARITY -> SCAFFOLD -> PARITY`.
- **Blackboard** (`core/blackboard.py`) — typed Pydantic state under registry keys (`core/schemas.py::Keys`), telemetry log with async subscribers (SSE), JSON snapshot/restore.
- **BaseAgent** (`core/base_agent.py`) — `requires`/`produces` preflight & postflight, `decide()` = deterministic default + LLM refinement + prompt hashing for provenance.
- **LLM providers** (`reposplit/llm/`) — `mock` (deterministic, CI), `anthropic` (Claude via official SDK), `watsonx` (IBM Granite with structured JSON mode and retry fallback), `langchain` (Groq/OpenAI/WatsonX). Never blocks on model outages: automatic graceful heuristic fallback.
- **Agents** (`reposplit/agents/*`) — one package each:
  - **Architect**: AST symbol/call graph, Louvain clustering, coupling metrics, Django & Flask router parsing.
  - **DataSplit**: Schema isolation, foreign key severance, Saga orchestrator, CQRS view synthesis, CDC sync daemon.
  - **Contract**: OpenAPI 3.0 & gRPC specs, typed downstream clients with circuit breakers & retry policies.
  - **Strangler**: Envoy & Kong canary routing, xDS runtime weight tuning, auto-rollback.
  - **Scaffold**: FastAPI services, Jinja2 templates, JWT security middleware, Docker Compose & Helm topology.
  - **Parity**: Automated differential test runner, semantic JSON diff, AST auto-healer.
  - **Governance**: DSSE envelope signing with Ed25519, FinOps cost/carbon calculator, executive HTML migration certificate.
- **Generators** (`reposplit/generators/`) — Jinja2 templates + `porting.py` (Flask/Django → FastAPI) + `scaffold_agent.py` + `cdc.py`.
- **API** (`reposplit/api/server.py`) — `POST /api/runs`, `GET /api/runs/{id}/events` (SSE), `/graph`, `/state/{key}`, `/approve`. The bundled `static/index.html` is a reference client for the real React/D3 dashboard.

Details: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) · agent contract: [docs/AGENTS.md](docs/AGENTS.md) · demo: [docs/DEMO_SCRIPT.md](docs/DEMO_SCRIPT.md)

## CLI

```
reposplit run REPO [--out out] [--provider auto|mock|anthropic|watsonx|langchain] [--model MODEL]
                   [--mode full|strangler --service NAME] [--gateway envoy|kong|both] [--canary 10]
                   [--live | --monolith-url U --services-url U] [--resume]
                   [--heal/--no-heal] [--max-heal 3] [--strict-parity] [--yes] [--strict-llm]
                   [--uuid-refs] [--signing-key k.pem]
reposplit graph REPO            topology only
reposplit parity --suite ... --monolith-url ... --services-url ...
reposplit verify PASSPORT
reposplit serve [--port 8765]
reposplit agents
```

### Advanced Modes & Flags
- **Strangler Mode:** Extract one service and keep the rest in the monolith:
  `reposplit run examples/shop_monolith --mode strangler --service catalog_service --yes`
- **Dual Gateway Support:** Generate Envoy, Kong 3.0, or both:
  `reposplit run examples/shop_monolith --gateway both --yes`
- **Fault-Tolerant Resumption:** Resume interrupted runs from saved Blackboard state:
  `reposplit run examples/shop_monolith --resume --yes`

## Using a real model

```bash
# IBM watsonx.ai (IBM Granite)
export WATSONX_API_KEY=...
export WATSONX_PROJECT_ID=...
reposplit run examples/shop_monolith --provider watsonx --model ibm/granite-3-8b-instruct --yes --live

# Anthropic Claude
export ANTHROPIC_API_KEY=...
reposplit run examples/shop_monolith --provider anthropic --yes --live
```

The Architect (cluster naming + risk), DataSplit (saga review) and Parity (auto-heal patches)
agents make structured decisions; Contract, Strangler, Scaffold and Governance are deterministic
by design so identical inputs give identical, attestable outputs. Every prompt hash and decision
origin is cryptographically sealed in the Migration Passport.

## Development

```bash
make test      # pytest (52 automated tests pass; REPOSPLIT_SKIP_LIVE=1 to skip subprocess test)
make lint      # ruff check
make demo      # offline run
```

CI (`.github/workflows/ci.yml`) runs lint, tests, an end-to-end demo and passport verification on
Python 3.11–3.13 and uploads `out/` as an artifact.

## Scope & Capabilities

- **Frameworks:** Flask, FastAPI, and Django ORM (`models.Model`, fields, and router paths).
- **Data Modernization:** Automated FK severance, Saga compensations, CQRS projections, and SQLite/Postgres CDC delta synchronization.
- **Gateway Orchestration:** Envoy weighted clusters (with xDS `/runtime_modify` API) and Kong 3.0 declarative config (`kong.yml`).
- **Security & FinOps:** JWT security context propagation (`JWT_SECRET`), verifiable Ed25519 DSSE envelopes, and cloud cost/carbon modeling.

License: Apache-2.0
