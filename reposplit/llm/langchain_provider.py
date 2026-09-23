"""LangChain provider for Groq.

Uses LangChain's `.with_structured_output(schema)` so every agent decision is
natively validated against its Pydantic schema before touching the Blackboard.
Adheres strictly to AGENTS.md: zero raw requests/httpx code.
"""

from __future__ import annotations

import os
from typing import TypeVar

from pydantic import BaseModel

from reposplit.llm.provider import LLMError, LLMProvider

T = TypeVar("T", bound=BaseModel)


class LangChainProvider(LLMProvider):
    name = "groq"

    def __init__(
        self,
        model: str,
    ) -> None:
        self.model = model
        api_key = os.environ.get("GROQ_API_KEY")
        if not api_key:
            raise LLMError("GROQ_API_KEY environment variable is required")
        try:
            from langchain_groq import ChatGroq
        except ImportError as exc:
            raise LLMError("langchain-groq not installed: pip install 'reposplit[langchain]'") from exc

        self._llm = ChatGroq(
            model=model,
            api_key=api_key,
            temperature=0.1,
            max_tokens=800,
        )

    async def complete_structured(
        self,
        *,
        system: str,
        user: str,
        schema: type[T],
        default: T,
        max_tokens: int = 16000,
    ) -> T:
        try:
            structured_llm = self._llm.with_structured_output(schema)
            messages = [
                ("system", system),
                ("user", user),
            ]
            res = await structured_llm.ainvoke(messages)
            if isinstance(res, schema):
                return res
            if isinstance(res, dict):
                return schema.model_validate(res)
            return schema.model_validate(res)
        except Exception as exc:
            raise LLMError(f"{self.name} structured completion failed: {exc}") from exc

    async def complete_text(self, *, system: str, user: str, max_tokens: int = 4096) -> str:
        try:
            messages = [
                ("system", system),
                ("user", user),
            ]
            res = await self._llm.ainvoke(messages)
            return str(res.content)
        except Exception as exc:
            raise LLMError(f"{self.name} text completion failed: {exc}") from exc
