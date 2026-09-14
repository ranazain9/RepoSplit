"""BaseAgent - the contract every specialised subagent implements.

Template method:
    run() -> preflight (required blackboard keys present)
          -> execute()  (agent-specific)
          -> postflight (declared keys were produced, validated against their schema)

Every LLM decision goes through `decide()`, which hashes the prompt for the Migration Passport,
supplies the deterministic default, and applies the fallback policy.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, TypeVar

from pydantic import BaseModel

from reposplit.core.blackboard import Blackboard, BlackboardKeyError
from reposplit.core.schemas import AgentResult, Phase, PromptRecord, RunConfig
from reposplit.llm.provider import LLMError, LLMProvider, prompt_sha256

T = TypeVar("T", bound=BaseModel)


@dataclass
class AgentContext:
    config: RunConfig
    llm: LLMProvider
    blackboard: Blackboard
    output_dir: Path
    repo_root: Path
    approval: asyncio.Event = field(default_factory=asyncio.Event)
    logger: logging.Logger = field(default_factory=lambda: logging.getLogger("reposplit"))


class AgentError(RuntimeError):
    pass


class BaseAgent(ABC):
    name: ClassVar[str] = "base"
    phase: ClassVar[Phase] = Phase.INGEST
    requires: ClassVar[tuple[str, ...]] = ()
    produces: ClassVar[tuple[str, ...]] = ()
    description: ClassVar[str] = ""
    uses_llm: ClassVar[bool] = False

    def __init__(self, ctx: AgentContext) -> None:
        self.ctx = ctx
        self.bb = ctx.blackboard
        self.config = ctx.config
        self.warnings: list[str] = []
        self.produced_files: list[str] = []

    # ---- lifecycle -----------------------------------------------------------------

    async def run(self) -> AgentResult:
        t0 = time.perf_counter()
        self.log(f"{self.name} starting", level="info")
        try:
            self._preflight()
            result = await self.execute()
            self._postflight()
        except (AgentError, BlackboardKeyError) as exc:
            self.log(str(exc), level="error")
            return AgentResult(
                agent=self.name,
                phase=self.phase,
                ok=False,
                summary=str(exc),
                warnings=self.warnings,
                duration_s=round(time.perf_counter() - t0, 3),
            )
        result.duration_s = round(time.perf_counter() - t0, 3)
        result.warnings = list(dict.fromkeys(result.warnings + self.warnings))
        result.produced = list(dict.fromkeys(result.produced + self.produced_files))
        self.log(result.summary, level="success" if result.ok else "error", metrics=result.metrics)
        return result

    @abstractmethod
    async def execute(self) -> AgentResult:  # pragma: no cover - abstract
        ...

    def _preflight(self) -> None:
        missing = [k for k in self.requires if not self.bb.has(k)]
        if missing:
            raise BlackboardKeyError(f"{self.name} preflight failed; missing keys: {missing}")

    def _postflight(self) -> None:
        missing = [k for k in self.produces if not self.bb.has(k)]
        if missing:
            raise AgentError(f"{self.name} postflight failed; did not produce: {missing}")

    # ---- helpers -------------------------------------------------------------------

    def log(self, message: str, level: str = "info", **payload: Any) -> None:
        self.bb.record(self.phase, self.name, message, level=level, **payload)
        getattr(self.ctx.logger, "warning" if level == "warning" else "info")("[%s] %s", self.name, message)

    def warn(self, message: str) -> None:
        self.warnings.append(message)
        self.log(message, level="warning")

    def ok(self, summary: str, **metrics: Any) -> AgentResult:
        return AgentResult(agent=self.name, phase=self.phase, ok=True, summary=summary, metrics=metrics)

    async def decide(
        self,
        *,
        system: str,
        user: str,
        schema: type[T],
        default: T,
        max_tokens: int = 16000,
    ) -> T:
        """Ask the LLM for a structured decision, recording the prompt hash for provenance."""
        llm = self.ctx.llm
        digest = prompt_sha256(system, user, schema.__name__)
        used_default = False
        try:
            decision = await llm.complete_structured(
                system=system, user=user, schema=schema, default=default, max_tokens=max_tokens
            )
        except LLMError as exc:
            if self.config.strict_llm:
                raise AgentError(f"LLM failure in {self.name} (strict mode): {exc}") from exc
            self.warn(f"LLM unavailable ({exc}); using deterministic default for {schema.__name__}")
            decision = default
            used_default = True
        if llm.is_mock:
            used_default = True
        self.bb.record_prompt(
            PromptRecord(agent=self.name, provider=llm.name, model=llm.model, sha256=digest, used_default=used_default)
        )
        self.log(
            f"decision {schema.__name__} via {llm.name}" + (" (heuristic default)" if used_default else ""),
            level="debug",
            prompt_sha256=digest,
        )
        return decision

    def write_artifact(self, relpath: str, content: str | bytes | BaseModel | dict | list) -> Path:
        target = self.ctx.output_dir / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, BaseModel):
            data: str | bytes = content.model_dump_json(indent=2, by_alias=True)
        elif isinstance(content, dict | list):
            data = json.dumps(content, indent=2)
        else:
            data = content
        if isinstance(data, bytes):
            target.write_bytes(data)
        else:
            target.write_text(data, encoding="utf-8", newline="\n")
        rel = relpath.replace("\\", "/")
        self.produced_files.append(rel)
        self.bb.register_artifact(rel)
        return target
