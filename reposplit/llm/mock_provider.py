"""Deterministic offline provider.

Returns the agent's heuristic default verbatim (after a schema round-trip so the same validation
path is exercised). Used in CI, in the demo when no API key is present, and in tests.
"""

from __future__ import annotations

from typing import TypeVar

from pydantic import BaseModel

from reposplit.llm.provider import LLMProvider

T = TypeVar("T", bound=BaseModel)


class MockProvider(LLMProvider):
    name = "mock"
    model = "deterministic-heuristics"

    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    async def complete_structured(
        self,
        *,
        system: str,
        user: str,
        schema: type[T],
        default: T,
        max_tokens: int = 16000,
    ) -> T:
        self.calls.append({"system": system, "user": user, "schema": schema.__name__})
        return schema.model_validate(default.model_dump())

    async def complete_text(self, *, system: str, user: str, max_tokens: int = 4096) -> str:
        self.calls.append({"system": system, "user": user, "schema": "text"})
        return "[mock provider] no LLM available - deterministic heuristics were used."

    @property
    def is_mock(self) -> bool:
        return True
