"""IBM watsonx.ai (Granite) provider - SCAFFOLD.

This is a minimal REST implementation against the watsonx.ai chat endpoint. It is intentionally
small so the team can swap in the official `ibm-watsonx-ai` SDK if preferred.

TODO(team): verify the endpoint version string and the response shape against the current
watsonx.ai API docs before relying on this in the demo. Env vars: WATSONX_API_KEY,
WATSONX_PROJECT_ID, WATSONX_URL, WATSONX_MODEL.
"""

from __future__ import annotations

import json
import os
from typing import TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from reposplit.llm.provider import LLMError, LLMProvider

T = TypeVar("T", bound=BaseModel)

IAM_URL = "https://iam.cloud.ibm.com/identity/token"


class WatsonxProvider(LLMProvider):
    name = "watsonx"

    def __init__(self, model: str) -> None:
        self.model = model
        self.api_key = os.environ.get("WATSONX_API_KEY", "")
        self.project_id = os.environ.get("WATSONX_PROJECT_ID", "")
        self.base_url = os.environ.get("WATSONX_URL", "https://us-south.ml.cloud.ibm.com").rstrip("/")
        self.api_version = os.environ.get("WATSONX_API_VERSION", "2024-05-31")
        if not self.api_key or not self.project_id:
            raise LLMError("WATSONX_API_KEY and WATSONX_PROJECT_ID are required for the watsonx provider")

    async def _iam_token(self, client: httpx.AsyncClient) -> str:
        resp = await client.post(
            IAM_URL,
            data={"grant_type": "urn:ibm:params:oauth:grant-type:apikey", "apikey": self.api_key},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if resp.status_code != 200:
            raise LLMError(f"IAM token exchange failed: {resp.status_code} {resp.text[:200]}")
        return resp.json()["access_token"]

    async def _chat(self, system: str, user: str, max_tokens: int) -> str:
        async with httpx.AsyncClient(timeout=120) as client:
            token = await self._iam_token(client)
            resp = await client.post(
                f"{self.base_url}/ml/v1/text/chat",
                params={"version": self.api_version},
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={
                    "model_id": self.model,
                    "project_id": self.project_id,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "max_tokens": max_tokens,
                    "temperature": 0,
                },
            )
        if resp.status_code != 200:
            raise LLMError(f"watsonx chat failed: {resp.status_code} {resp.text[:200]}")
        data = resp.json()
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as exc:
            raise LLMError(f"unexpected watsonx response shape: {json.dumps(data)[:200]}") from exc

    async def complete_structured(
        self,
        *,
        system: str,
        user: str,
        schema: type[T],
        default: T,
        max_tokens: int = 16000,
    ) -> T:
        schema_json = json.dumps(schema.model_json_schema())
        system_with_schema = (
            f"{system}\n\nRespond with ONLY a JSON object matching this JSON schema, no prose:\n{schema_json}"
        )
        text = await self._chat(system_with_schema, user, max_tokens)
        text = text.strip()
        if text.startswith("```"):
            text = text.strip("`")
            text = text.split("\n", 1)[1] if "\n" in text else text
        try:
            return schema.model_validate_json(text)
        except ValidationError as exc:
            raise LLMError(f"watsonx returned invalid {schema.__name__}: {exc}") from exc

    async def complete_text(self, *, system: str, user: str, max_tokens: int = 4096) -> str:
        return await self._chat(system, user, max_tokens)
