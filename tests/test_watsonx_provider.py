"""Tests for IBM watsonx.ai (Granite) provider."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from pydantic import BaseModel

from reposplit.llm.provider import LLMError, build_provider
from reposplit.llm.watsonx_provider import WatsonxProvider


class SampleDecision(BaseModel):
    name: str
    confidence: float


def test_watsonx_provider_requires_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WATSONX_API_KEY", raising=False)
    monkeypatch.delenv("WATSONX_PROJECT_ID", raising=False)

    with pytest.raises(LLMError, match="WATSONX_API_KEY and WATSONX_PROJECT_ID are required"):
        WatsonxProvider(model="ibm/granite-3-8b-instruct")


def test_build_provider_watsonx_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WATSONX_API_KEY", "test-key")
    monkeypatch.setenv("WATSONX_PROJECT_ID", "test-project")

    provider = build_provider("watsonx", model="ibm/granite-3-8b-instruct")
    assert isinstance(provider, WatsonxProvider)
    assert provider.name == "watsonx"
    assert provider.model == "ibm/granite-3-8b-instruct"


@pytest.mark.asyncio
async def test_watsonx_complete_structured_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WATSONX_API_KEY", "test-key")
    monkeypatch.setenv("WATSONX_PROJECT_ID", "test-project")

    provider = WatsonxProvider(model="ibm/granite-3-8b-instruct")

    mock_iam_resp = AsyncMock()
    mock_iam_resp.status_code = 200
    mock_iam_resp.json = lambda: {"access_token": "mock-token-xyz"}

    mock_chat_resp = AsyncMock()
    mock_chat_resp.status_code = 200
    mock_chat_resp.json = lambda: {
        "results": [{"generated_text": '```json\n{"name": "order_service", "confidence": 0.98}\n```'}]
    }

    async def mock_post(url: str, **kwargs):
        if "iam.cloud.ibm.com" in url:
            return mock_iam_resp
        return mock_chat_resp

    with patch("httpx.AsyncClient.post", side_effect=mock_post):
        default = SampleDecision(name="default_svc", confidence=0.5)
        res = await provider.complete_structured(
            system="system prompt",
            user="user prompt",
            schema=SampleDecision,
            default=default,
        )
        assert res.name == "order_service"
        assert res.confidence == 0.98


@pytest.mark.asyncio
async def test_watsonx_iam_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WATSONX_API_KEY", "invalid-key")
    monkeypatch.setenv("WATSONX_PROJECT_ID", "test-project")

    provider = WatsonxProvider(model="ibm/granite-3-8b-instruct")

    mock_iam_resp = AsyncMock()
    mock_iam_resp.status_code = 401
    mock_iam_resp.text = "Unauthorized apikey"

    with (
        patch("httpx.AsyncClient.post", return_value=mock_iam_resp),
        pytest.raises(LLMError, match="IAM token exchange failed"),
    ):
        await provider.complete_text(system="sys", user="user")
