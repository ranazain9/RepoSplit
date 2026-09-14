# Agent contract (short form)

See `CONTRIBUTING.md` → "The agent contract" for the full version. Summary:

- Subclass `reposplit.core.base_agent.BaseAgent`; set `name`, `phase`, `requires`, `produces`, `uses_llm`.
- Read with `self.bb.require(Keys.X, Model)`, write with `self.bb.put(Keys.Y, model)`.
- Compute a deterministic default, then `await self.decide(system=, user=, schema=, default=)`.
- Emit files with `self.write_artifact(relpath, content)`; log with `self.log()` / `self.warn()`.
- Return `self.ok("summary", **metrics)`; raise `AgentError` for unrecoverable problems.
- Register in `reposplit/core/registry.py`; add a phase in `core/fsm.py` if needed; add a gate in `core/supervisor.py` if the output must be validated.

Run `reposplit agents` to see the live DAG.
