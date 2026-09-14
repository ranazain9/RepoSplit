"""Claude provider via the official Anthropic SDK.

IBM Bob 2.0 reasons with Claude under the hood; this is the direct-API path for running the
engine outside the Bob IDE. Uses `messages.parse` so every decision is validated against the
agent's Pydantic schema before it touches the Blackboard.

Install with: pip install "reposplit[anthropic]"
"""

from __future__ import annotations

from typing import TypeVar

from pydantic import BaseModel

from reposplit.llm.provider import DEFAULT_ANTHROPIC_MODEL, LLMError, LLMProvider

T = TypeVar("T", bound=BaseModel)


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(self, model: str = DEFAULT_ANTHROPIC_MODEL, effort: str = "high") -> None:
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - exercised only without the extra installed
            raise LLMError("anthropic SDK not installed: pip install 'reposplit[anthropic]'") from exc
        self._anthropic = anthropic
        # Credentials resolve from ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN / `ant auth login` profile.
        self._client = anthropic.AsyncAnthropic()
        self.model = model
        self.effort = effort

    async def complete_structured(
        self,
        *,
        system: str,
        user: str,
        schema: type[T],
        default: T,
        max_tokens: int = 16000,
    ) -> T:
        a = self._anthropic
        try:
            response = await self._client.messages.parse(
                model=self.model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_format=schema,
                thinking={"type": "adaptive"},
                output_config={"effort": self.effort},
            )
        except a.RateLimitError as exc:
            raise LLMError(f"rate limited: {exc}") from exc
        except a.APIStatusError as exc:
            raise LLMError(f"api error {exc.status_code}: {exc.message}") from exc
        except a.APIConnectionError as exc:
            raise LLMError(f"connection error: {exc}") from exc

        if response.stop_reason == "refusal":
            detail = getattr(response, "stop_details", None)
            raise LLMError(f"model refused: {getattr(detail, 'explanation', '') or 'no explanation'}")
        if response.stop_reason == "max_tokens":
            raise LLMError("response truncated at max_tokens")
        parsed = response.parsed_output
        if parsed is None:
            raise LLMError("no structured output returned")
        return parsed

    async def complete_text(self, *, system: str, user: str, max_tokens: int = 4096) -> str:
        a = self._anthropic
        try:
            response = await self._client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
                thinking={"type": "adaptive"},
                output_config={"effort": self.effort},
            )
        except (a.APIStatusError, a.APIConnectionError) as exc:
            raise LLMError(str(exc)) from exc
        if response.stop_reason == "refusal":
            raise LLMError("model refused")
        return "".join(block.text for block in response.content if block.type == "text")
