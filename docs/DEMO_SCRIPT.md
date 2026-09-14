# 3-minute demo script (what to run, what to say)

Prep: `pip install -e ".[all]"`, `rm -rf out`, two terminals, dashboard open (`reposplit serve`).

## 0:00–0:45 The agony
- Show `examples/shop_monolith/models.py` (God file, 5 tables, FKs across domains) and
  `routes/checkout.py::create_order` (user → stock → payment in one transaction).
- Say: "manual split = 6 months / $250k, and the shared DB is why teams never start."

## 0:45–1:45 The AI intervention
- Terminal: `reposplit run examples/shop_monolith --provider mock --live` (leave the approval gate ON).
- Dashboard: click **Run**, watch the Architect log, click **Untangle** — grey hairball → 4 coloured services, severed edges dashed red.
- Point at the pause: "human oversight gate — the plan is on disk before a line of code is generated". Click **Approve**.
- Show `out/data/isolated_schemas/order_service/models.py` (soft reference comment) and `out/data/saga_orchestrator.py`
  (`CreateOrderSaga`: ReserveStock ⇄ release_stock, ChargePayment ⇄ refund_payment).

## 1:45–2:30 The concrete proof
- The run boots the monolith + 4 FastAPI services as real processes and runs the differential suite.
- Show the parity table: 19/22 byte-identical, `create_order` 201 == 201 with the same total (tax survived the port),
  `cancel_order` compensations ran across services.
- Show the 3 failures = the cross-DB JOIN, and `reports/parity_needs_human.md` naming the CQRS projection.
  "The engine tells you exactly what it could not prove."

## 2:30–3:00 The enterprise vision
- `reposplit verify out/reports/migration_passport.json` — signature valid, digests match.
- Open the passport: prompt hashes, provider/model, coupling 0.77 → 0.27, parity 86%, compliance controls.
- `out/gateway/envoy.yaml` (10% canary, outlier ejection) and `out/helm/reposplit/values.yaml` (OpenShift + Instana endpoint).
- Close: "Mono2Micro told architects where to cut. RepoSplit executes the cut, proves it, and signs it."

## Judge Q&A one-liners
- *Why not Cursor?* File-level editors are blind to the schema; we partition data, sever FKs, generate sagas, and prove parity.
- *Consistency?* Sagas with real compensations + outbox; see `data/saga_orchestrator.py`, the demo cancels an order across three services.
- *Regressions?* Differential parity across real HTTP boundaries, normalized diffs, auto-heal loop with the legacy source as ground truth, unhealable cases escalated.
- *Big-bang risk?* Strangler mode: `--mode strangler --service catalog_service`; Envoy weights start at 10%, rollback is a weight change.
