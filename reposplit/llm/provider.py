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
DEFAULT_GROQ_MODEL = "qwen/qwen3.8-27b"


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


class CascadeProvider(LLMProvider):
    """Resilient provider that cascades across multiple backends with failover."""

    def __init__(self, providers: list[LLMProvider]) -> None:
        if not providers:
            raise ValueError("CascadeProvider requires at least one provider")
        self.providers = providers
        self.name = providers[0].name
        self.model = providers[0].model

    @property
    def is_mock(self) -> bool:
        return all(p.is_mock for p in self.providers)

    async def complete_structured(
        self,
        *,
        system: str,
        user: str,
        schema: type[T],
        default: T,
        max_tokens: int = 16000,
    ) -> T:
        last_exc: Exception | None = None
        for p in self.providers:
            try:
                res = await p.complete_structured(
                    system=system, user=user, schema=schema, default=default, max_tokens=max_tokens
                )
                self.name = p.name
                self.model = p.model
                return res
            except Exception as exc:
                last_exc = exc
                continue
        raise LLMError(f"all providers in cascade failed: {last_exc}") from last_exc

    async def complete_text(self, *, system: str, user: str, max_tokens: int = 4096) -> str:
        last_exc: Exception | None = None
        for p in self.providers:
            try:
                res = await p.complete_text(system=system, user=user, max_tokens=max_tokens)
                self.name = p.name
                self.model = p.model
                return res
            except Exception as exc:
                last_exc = exc
                continue
        raise LLMError(f"all providers in cascade failed: {last_exc}") from last_exc


def build_provider(kind: str = "auto", model: str | None = None) -> LLMProvider:
    """Factory used by the CLI, the API server and tests.

    auto -> groq | anthropic | watsonx when their API keys are set, else mock.
    cascade -> Groq -> Anthropic -> WatsonX -> Mock failover.
    """
    if "PYTEST_CURRENT_TEST" not in os.environ:
        try:
            from dotenv import load_dotenv

            load_dotenv()
        except ImportError:
            pass

    kind = (kind or os.environ.get("REPOSPLIT_PROVIDER", "auto")).lower()
    model = model or os.environ.get("REPOSPLIT_MODEL") or None

    if kind == "cascade":
        chain: list[LLMProvider] = []
        if os.environ.get("GROQ_API_KEY"):
            from reposplit.llm.langchain_provider import LangChainProvider

            chain.append(LangChainProvider(model=model or os.environ.get("GROQ_MODEL") or DEFAULT_GROQ_MODEL))
        if os.environ.get("ANTHROPIC_API_KEY"):
            from reposplit.llm.anthropic_provider import AnthropicProvider

            chain.append(AnthropicProvider(model=model or DEFAULT_ANTHROPIC_MODEL))
        if os.environ.get("WATSONX_API_KEY"):
            from reposplit.llm.watsonx_provider import WatsonxProvider

            chain.append(WatsonxProvider(model=model or os.environ.get("WATSONX_MODEL", DEFAULT_WATSONX_MODEL)))
        from reposplit.llm.mock_provider import MockProvider

        chain.append(MockProvider())
        return CascadeProvider(chain)

    if kind == "auto":
        if os.environ.get("GROQ_API_KEY"):
            kind = "groq"
        elif os.environ.get("ANTHROPIC_API_KEY"):
            kind = "anthropic"
        elif os.environ.get("WATSONX_API_KEY"):
            kind = "watsonx"
        else:
            kind = "mock"

    if kind == "mock":
        from reposplit.llm.mock_provider import MockProvider

        return MockProvider()
    if kind == "groq":
        from reposplit.llm.langchain_provider import LangChainProvider

        env_model = os.environ.get("GROQ_MODEL")
        return LangChainProvider(model=model or env_model or DEFAULT_GROQ_MODEL)
    if kind == "anthropic":
        from reposplit.llm.anthropic_provider import AnthropicProvider

        return AnthropicProvider(model=model or DEFAULT_ANTHROPIC_MODEL)
    if kind == "watsonx":
        from reposplit.llm.watsonx_provider import WatsonxProvider

        return WatsonxProvider(model=model or os.environ.get("WATSONX_MODEL", DEFAULT_WATSONX_MODEL))
    raise ValueError(f"unknown provider: {kind}")
