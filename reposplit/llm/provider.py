"""LLM provider abstraction.

Design rule: the deterministic tooling (AST, NetworkX, schema parsing) is always the source of
truth and produces a *default* decision. The LLM is asked to refine that default and must answer
in a strict Pydantic schema. If the model is unavailable, misbehaves, or refuses, the engine falls
back to the default (unless `strict_llm` is set) - the pipeline never blocks on an LLM.
"""

from __future__ import annotations

import hashlib
import os
from abc import ABC, abstractmethod
from typing import TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

DEFAULT_ANTHROPIC_MODEL = "claude-opus-5"
DEFAULT_WATSONX_MODEL = "ibm/granite-3-8b-instruct"


class LLMError(RuntimeError):
    """Raised by providers for any failure the engine should treat as 'no usable answer'."""


def prompt_sha256(system: str, user: str, schema_name: str) -> str:
    return hashlib.sha256(f"{schema_name}\n{system}\n---\n{user}".encode()).hexdigest()


class LLMProvider(ABC):
    name: str = "base"
    model: str = ""

    @abstractmethod
    async def complete_structured(
        self,
        *,
        system: str,
        user: str,
        schema: type[T],
        default: T,
        max_tokens: int = 16000,
    ) -> T:
        """Return an instance of `schema`. Implementations may raise LLMError."""

    async def complete_text(self, *, system: str, user: str, max_tokens: int = 4096) -> str:
        raise LLMError(f"{self.name} does not implement free-text completion")

    @property
    def is_mock(self) -> bool:
        return False


def build_provider(kind: str = "auto", model: str | None = None) -> LLMProvider:
    """Factory used by the CLI, the API server and tests.

    auto -> anthropic when ANTHROPIC_API_KEY is set, else mock.
    """
    kind = (kind or os.environ.get("REPOSPLIT_PROVIDER", "auto")).lower()
    model = model or os.environ.get("REPOSPLIT_MODEL") or None

    if kind == "auto":
        kind = "anthropic" if os.environ.get("ANTHROPIC_API_KEY") else "mock"

    if kind == "mock":
        from reposplit.llm.mock_provider import MockProvider

        return MockProvider()
    if kind == "anthropic":
        from reposplit.llm.anthropic_provider import AnthropicProvider

        return AnthropicProvider(model=model or DEFAULT_ANTHROPIC_MODEL)
    if kind == "watsonx":
        from reposplit.llm.watsonx_provider import WatsonxProvider

        return WatsonxProvider(model=model or os.environ.get("WATSONX_MODEL", DEFAULT_WATSONX_MODEL))
    raise ValueError(f"unknown provider: {kind}")
