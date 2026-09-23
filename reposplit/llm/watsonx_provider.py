"""IBM watsonx.ai (Granite) provider.

Calls the watsonx.ai REST chat endpoint and parses structured JSON responses.
Uses the official /ml/v1/text/chat endpoint (API version 2024-05-31).

Required env vars: WATSONX_API_KEY, WATSONX_PROJECT_ID
Optional env vars: WATSONX_URL (default: https://us-south.ml.cloud.ibm.com)
                   WATSONX_MODEL (default: ibm/granite-3-8b-instruct)
                   WATSONX_API_VERSION (default: 2024-05-31)
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
_DEFAULT_MODEL = "ibm/granite-3-8b-instruct"
_DEFAULT_VERSION = "2024-05-31"
_DEFAULT_BASE_URL = "https://us-south.ml.cloud.ibm.com"


class WatsonxProvider(LLMProvider):
    name = "watsonx"

    def __init__(self, model: str) -> None:
        self.model = model or os.environ.get("WATSONX_MODEL", _DEFAULT_MODEL)
        self.api_key = os.environ.get("WATSONX_API_KEY", "")
        self.project_id = os.environ.get("WATSONX_PROJECT_ID", "")
        self.base_url = os.environ.get("WATSONX_URL", _DEFAULT_BASE_URL).rstrip("/")
        self.api_version = os.environ.get("WATSONX_API_VERSION", _DEFAULT_VERSION)
        if not self.api_key or not self.project_id:
            raise LLMError("WATSONX_API_KEY and WATSONX_PROJECT_ID are required for the watsonx provider")

    async def _iam_token(self, client: httpx.AsyncClient) -> str:
        resp = await client.post(
            IAM_URL,
            data={"grant_type": "urn:ibm:params:oauth:grant-type:apikey", "apikey": self.api_key},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
        )
        if resp.status_code != 200:
            raise LLMError(f"IAM token exchange failed: {resp.status_code} {resp.text[:200]}")
        return resp.json()["access_token"]

    async def _chat(self, system: str, user: str, max_tokens: int, json_mode: bool = False) -> str:
        async with httpx.AsyncClient(timeout=120) as client:
            token = await self._iam_token(client)
            payload: dict = {
                "model_id": self.model,
                "project_id": self.project_id,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "parameters": {
                    "max_new_tokens": max_tokens,
                    "temperature": 0,
                    "decoding_method": "greedy",
                },
            }
            if json_mode:
                # Request JSON output mode where supported by the model
                payload["response_format"] = {"type": "json_object"}
            resp = await client.post(
                f"{self.base_url}/ml/v1/text/chat",
                params={"version": self.api_version},
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json=payload,
            )
        if resp.status_code != 200:
            raise LLMError(f"watsonx chat failed: {resp.status_code} {resp.text[:200]}")
        data = resp.json()
        try:
            # Verified response shape: results[0].generated_text (text/chat endpoint)
            # Some versions return choices[0].message.content (OpenAI-compat mode)
            if "results" in data:
                return data["results"][0]["generated_text"]
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as exc:
            raise LLMError(f"unexpected watsonx response shape: {json.dumps(data)[:300]}") from exc

    @staticmethod
    def _strip_fences(text: str) -> str:
        """Strip markdown code fences that some models prepend to JSON output."""
        text = text.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            # Drop opening fence (``` or ```json) and closing ```
            start = 1 if len(lines) > 1 else 0
            end = -1 if lines[-1].strip() == "```" else len(lines)
            text = "\n".join(lines[start:end]).strip()
        return text

    async def complete_structured(
        self,
        *,
        system: str,
        user: str,
        schema: type[T],
        default: T,
        max_tokens: int = 16000,
    ) -> T:
        schema_json = json.dumps(schema.model_json_schema(), indent=2)
        system_with_schema = (
            f"{system}\n\nRespond with ONLY a valid JSON object matching this JSON Schema. "
            f"No prose, no markdown fences, no explanation:\n{schema_json}"
        )
        text = await self._chat(system_with_schema, user, max_tokens, json_mode=True)
        text = self._strip_fences(text)
        try:
            return schema.model_validate_json(text)
        except (ValidationError, ValueError):
            # Second attempt: try to extract the first {...} block from the response
            import re
            m = re.search(r"\{[\s\S]+\}", text)
            if m:
                try:
                    return schema.model_validate_json(m.group(0))
                except (ValidationError, ValueError):
                    pass
            raise LLMError(f"watsonx returned invalid {schema.__name__}: {text[:400]}") from None

    async def complete_text(self, *, system: str, user: str, max_tokens: int = 4096) -> str:
        return await self._chat(system, user, max_tokens)
