"""Tests for the LangChain Groq provider."""

from __future__ import annotations

import pytest

from reposplit.llm.langchain_provider import LangChainProvider
from reposplit.llm.provider import LLMError, build_provider


def test_langchain_provider_requires_groq_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GROQ_API_KEY", raising=False)

    with pytest.raises(LLMError, match="GROQ_API_KEY"):
        LangChainProvider(model="llama-3.3-70b-versatile")


def test_build_provider_auto_picks_groq(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "test-groq-key")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("WATSONX_API_KEY", raising=False)

    provider = build_provider("auto")
    assert isinstance(provider, LangChainProvider)
    assert provider.name == "groq"
    assert provider.model == "qwen/qwen3.8-27b"


def test_build_provider_groq_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "test-groq-key")

    provider = build_provider("groq", model="llama-3.1-8b-instant")
    assert isinstance(provider, LangChainProvider)
    assert provider.name == "groq"
    assert provider.model == "llama-3.1-8b-instant"


def test_build_provider_auto_falls_back_to_mock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("WATSONX_API_KEY", raising=False)

    provider = build_provider("auto")
    assert provider.is_mock is True
