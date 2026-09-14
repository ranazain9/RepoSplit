"""Supervisor phase machine.

A small explicit FSM so the execution DAG is auditable: every transition is recorded with a
reason, illegal transitions raise, and the auto-healing loop (PARITY -> SCAFFOLD -> PARITY) is
the only permitted backward edge.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from reposplit.core.schemas import Phase, utcnow_iso

PHASE_ORDER: list[Phase] = [
    Phase.INGEST,
    Phase.ARCHITECT,
    Phase.DATA,
    Phase.CONTRACT,
    Phase.STRANGLER,
    Phase.SCAFFOLD,
    Phase.PARITY,
    Phase.GOVERNANCE,
    Phase.COMPLETE,
]

_FORWARD = {PHASE_ORDER[i]: PHASE_ORDER[i + 1] for i in range(len(PHASE_ORDER) - 1)}

TRANSITIONS: dict[Phase, set[Phase]] = {p: {nxt, Phase.FAILED} for p, nxt in _FORWARD.items()}
TRANSITIONS[Phase.PARITY].add(Phase.SCAFFOLD)  # auto-heal loop: re-scaffold patched services, re-test
TRANSITIONS[Phase.COMPLETE] = set()
TRANSITIONS[Phase.FAILED] = set()


class IllegalTransition(RuntimeError):
    pass


@dataclass
class Transition:
    src: Phase
    dst: Phase
    reason: str
    ts: str = field(default_factory=utcnow_iso)


@dataclass
class PhaseMachine:
    state: Phase = Phase.INGEST
    history: list[Transition] = field(default_factory=list)

    def can(self, dst: Phase) -> bool:
        return dst in TRANSITIONS.get(self.state, set())

    def advance(self, dst: Phase, reason: str = "") -> Phase:
        if not self.can(dst):
            raise IllegalTransition(f"{self.state} -> {dst} is not a legal phase transition")
        self.history.append(Transition(self.state, dst, reason))
        self.state = dst
        return self.state

    def next_phase(self) -> Phase | None:
        return _FORWARD.get(self.state)

    def fail(self, reason: str) -> Phase:
        if self.state in (Phase.COMPLETE, Phase.FAILED):
            return self.state
        self.history.append(Transition(self.state, Phase.FAILED, reason))
        self.state = Phase.FAILED
        return self.state

    @property
    def terminal(self) -> bool:
        return self.state in (Phase.COMPLETE, Phase.FAILED)
