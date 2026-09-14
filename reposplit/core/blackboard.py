"""Central Context State Store (the "Blackboard").

All agents read and write here; nothing is passed agent-to-agent directly. The Blackboard also
owns the telemetry event log, which the dashboard streams over SSE, and it can be persisted to
JSON after every phase so a run is resumable and auditable.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel

from reposplit.core.schemas import Phase, PromptRecord, TelemetryEvent

T = TypeVar("T", bound=BaseModel)


class BlackboardKeyError(KeyError):
    pass


def _to_jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", by_alias=True)
    if isinstance(value, list):
        return [_to_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: _to_jsonable(v) for k, v in value.items()}
    return value


class Blackboard:
    def __init__(self, run_id: str, output_dir: str | Path) -> None:
        self.run_id = run_id
        self.output_dir = Path(output_dir)
        self._data: dict[str, Any] = {}
        self._events: list[TelemetryEvent] = []
        self._prompts: list[PromptRecord] = []
        self._artifacts: list[str] = []
        self._subscribers: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = []
        self._lock = threading.RLock()
        self._seq = 0

    # ---- typed state ---------------------------------------------------------------

    def put(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = value

    def has(self, key: str) -> bool:
        with self._lock:
            return key in self._data

    def keys(self) -> list[str]:
        with self._lock:
            return sorted(self._data)

    def get(self, key: str, model: type[T] | None = None, default: Any = None) -> Any:
        with self._lock:
            if key not in self._data:
                return default
            value = self._data[key]
        if model is not None and not isinstance(value, model):
            value = model.model_validate(value)
            self.put(key, value)
        return value

    def require(self, key: str, model: type[T]) -> T:
        if not self.has(key):
            raise BlackboardKeyError(f"required blackboard key missing: {key}")
        return self.get(key, model)

    # ---- telemetry -----------------------------------------------------------------

    def record(
        self,
        phase: Phase,
        agent: str,
        message: str,
        level: str = "info",
        **payload: Any,
    ) -> TelemetryEvent:
        with self._lock:
            self._seq += 1
            event = TelemetryEvent(
                seq=self._seq, phase=phase, agent=agent, level=level, message=message, payload=_to_jsonable(payload)
            )
            self._events.append(event)
            subscribers = list(self._subscribers)
        for loop, queue in subscribers:
            with contextlib.suppress(RuntimeError):  # subscriber's loop already closed
                loop.call_soon_threadsafe(queue.put_nowait, event)
        return event

    def events(self) -> list[TelemetryEvent]:
        with self._lock:
            return list(self._events)

    def subscribe(self) -> asyncio.Queue:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        with self._lock:
            self._subscribers.append((loop, queue))
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        with self._lock:
            self._subscribers = [(loop, q) for loop, q in self._subscribers if q is not queue]

    # ---- provenance ----------------------------------------------------------------

    def record_prompt(self, record: PromptRecord) -> None:
        with self._lock:
            self._prompts.append(record)

    def prompts(self) -> list[PromptRecord]:
        with self._lock:
            return list(self._prompts)

    def register_artifact(self, relpath: str) -> None:
        with self._lock:
            if relpath not in self._artifacts:
                self._artifacts.append(relpath)

    def artifacts(self) -> list[str]:
        with self._lock:
            return list(self._artifacts)

    # ---- persistence ---------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "run_id": self.run_id,
                "data": {k: _to_jsonable(v) for k, v in self._data.items()},
                "prompts": [p.model_dump(mode="json") for p in self._prompts],
                "artifacts": list(self._artifacts),
                "event_count": len(self._events),
            }

    def save(self, path: str | Path | None = None) -> Path:
        target = Path(path) if path else self.output_dir / ".reposplit" / "blackboard.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.snapshot(), indent=2), encoding="utf-8")
        events_path = target.parent / "events.jsonl"
        with events_path.open("w", encoding="utf-8") as fh:
            for ev in self.events():
                fh.write(ev.model_dump_json() + "\n")
        return target

    @classmethod
    def load(cls, path: str | Path, output_dir: str | Path | None = None) -> Blackboard:
        path = Path(path)
        raw = json.loads(path.read_text(encoding="utf-8"))
        bb = cls(raw["run_id"], output_dir or path.parent.parent)
        bb._data = dict(raw.get("data", {}))
        bb._prompts = [PromptRecord.model_validate(p) for p in raw.get("prompts", [])]
        bb._artifacts = list(raw.get("artifacts", []))
        events_path = path.parent / "events.jsonl"
        if events_path.exists():
            for line in events_path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    bb._events.append(TelemetryEvent.model_validate_json(line))
            bb._seq = len(bb._events)
        return bb
