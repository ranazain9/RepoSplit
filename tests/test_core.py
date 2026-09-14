"""Blackboard, FSM and BaseAgent contract tests."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reposplit.core.base_agent import AgentContext, BaseAgent
from reposplit.core.blackboard import Blackboard, BlackboardKeyError
from reposplit.core.fsm import IllegalTransition, PhaseMachine
from reposplit.core.schemas import AgentResult, Phase, RepoManifest, RunConfig
from reposplit.llm.mock_provider import MockProvider


def test_blackboard_typed_roundtrip(tmp_path: Path) -> None:
    bb = Blackboard("r1", tmp_path)
    manifest = RepoManifest(root="x", file_count=1, python_files=1, total_loc=10, tree_sha256="abc", languages={".py": 1})
    bb.put("repo.manifest", manifest)
    bb.record(Phase.INGEST, "supervisor", "hello", answer=42)
    path = bb.save()
    loaded = Blackboard.load(path)
    assert loaded.require("repo.manifest", RepoManifest) == manifest
    assert loaded.events()[0].payload == {"answer": 42}
    with pytest.raises(BlackboardKeyError):
        loaded.require("missing", RepoManifest)


def test_blackboard_subscribers_receive_live_events(tmp_path: Path) -> None:
    async def main() -> None:
        bb = Blackboard("r2", tmp_path)
        q = bb.subscribe()
        bb.record(Phase.ARCHITECT, "architect", "tick")
        ev = await asyncio.wait_for(q.get(), timeout=1)
        assert ev.message == "tick" and ev.seq == 1
        bb.unsubscribe(q)

    asyncio.run(main())


def test_fsm_only_allows_forward_and_heal_loop() -> None:
    fsm = PhaseMachine()
    for phase in (Phase.ARCHITECT, Phase.DATA, Phase.CONTRACT, Phase.STRANGLER, Phase.SCAFFOLD, Phase.PARITY):
        fsm.advance(phase)
    assert fsm.can(Phase.SCAFFOLD)  # auto-heal backward edge
    fsm.advance(Phase.SCAFFOLD, "heal")
    fsm.advance(Phase.PARITY)
    with pytest.raises(IllegalTransition):
        fsm.advance(Phase.ARCHITECT)
    fsm.advance(Phase.GOVERNANCE)
    fsm.advance(Phase.COMPLETE)
    assert fsm.terminal and len(fsm.history) == 10


def test_base_agent_preflight_and_postflight(tmp_path: Path) -> None:
    class Needy(BaseAgent):
        name = "needy"
        phase = Phase.ARCHITECT
        requires = ("nope",)
        produces = ("out",)

        async def execute(self) -> AgentResult:
            return self.ok("ran")

    class Forgetful(Needy):
        requires = ()

    cfg = RunConfig(repo_path=".", output_dir=str(tmp_path))
    ctx = AgentContext(config=cfg, llm=MockProvider(), blackboard=Blackboard("r3", tmp_path), output_dir=tmp_path, repo_root=tmp_path)
    r1 = asyncio.run(Needy(ctx).run())
    assert not r1.ok and "missing keys" in r1.summary
    r2 = asyncio.run(Forgetful(ctx).run())
    assert not r2.ok and "did not produce" in r2.summary


def test_decide_records_prompt_hash(tmp_path: Path) -> None:
    from reposplit.core.schemas import ArchitectDecision

    class Thinker(BaseAgent):
        name = "thinker"
        phase = Phase.ARCHITECT

        async def execute(self) -> AgentResult:
            d = await self.decide(system="s", user="u", schema=ArchitectDecision, default=ArchitectDecision(clusters=[]))
            assert isinstance(d, ArchitectDecision)
            return self.ok("ok")

    cfg = RunConfig(repo_path=".", output_dir=str(tmp_path))
    bb = Blackboard("r4", tmp_path)
    ctx = AgentContext(config=cfg, llm=MockProvider(), blackboard=bb, output_dir=tmp_path, repo_root=tmp_path)
    assert asyncio.run(Thinker(ctx).run()).ok
    prompts = bb.prompts()
    assert len(prompts) == 1 and len(prompts[0].sha256) == 64 and prompts[0].used_default
