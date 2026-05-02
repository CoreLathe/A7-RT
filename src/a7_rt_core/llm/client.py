"""
A7-RT LLM Transport

Thin wrapper around the OpenRouter API (via openai SDK). Every module that
makes LLM calls uses this. Pure API plumbing — no knowledge of A7, roles,
protocols, or project structure.

Usage:
    from openai import OpenAI
    from llm_client import LLMClient, LLMError

    client = OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key="sk-or-...",
    )
    llm = LLMClient(client, model="anthropic/claude-sonnet-4-6")
    text = llm.call(system="You are helpful.", user="Hello.")
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)


# Rate limit retry configuration
RATE_LIMIT_RETRY_SECONDS = 60
REQUEST_TIMEOUT_SECONDS = 120  # Hard ceiling on API calls


# ---------------------------------------------------------------------------
# LLMError
# ---------------------------------------------------------------------------


class LLMError(Exception):
    """
    Raised when the LLM API fails unrecoverably after all retries.

    Attributes
    ----------
    cause       : the last exception raised by the SDK
    attempt_count : number of attempts made (equal to max_retries)
    model       : the model ID that was targeted
    """

    def __init__(self, cause: BaseException, attempt_count: int, model: str) -> None:
        self.cause = cause
        self.attempt_count = attempt_count
        self.model = model
        super().__init__(
            f"LLM API failed after {attempt_count} attempt(s) on model {model!r}: {cause}"
        )


# ---------------------------------------------------------------------------
# LLMClient
# ---------------------------------------------------------------------------


class LLMClient:
    """
    Thin OpenRouter (openai SDK) wrapper with retry logic.

    Parameters
    ----------
    client      : openai.OpenAI instance pointed at OpenRouter (injected; not
                  imported at module level so the module loads in test contexts
                  without the SDK installed)
    model       : OpenRouter model ID for all calls made through this client
    max_retries : number of attempts before raising LLMError
    base_delay  : initial backoff delay in seconds; doubles each attempt
    """

    def __init__(
        self,
        client: Any,
        model: str = "anthropic/claude-sonnet-4-6",
        max_retries: int = 3,
        base_delay: float = 1.0,
    ) -> None:
        self._client = client
        self._model = model
        self._max_retries = max_retries
        self._base_delay = base_delay

    def call(
        self,
        system: str,
        user: str,
        max_tokens: int = 4096,
        tools: Optional[list[dict]] = None,
    ) -> str:
        """
        Single LLM API call with exponential backoff retry.

        Parameters
        ----------
        system     : system prompt
        user       : user message
        max_tokens : maximum output tokens
        tools      : optional tool definitions forwarded to the API as-is

        Returns
        -------
        str : raw text content of the response

        Raises
        ------
        LLMError               : after max_retries failed attempts
        LLMAuthenticationError : immediately on authentication failure (not retried)
        """
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        result = self.call_with_messages(messages, max_tokens, tools)
        return result.content

    def call_with_messages(
        self,
        messages: list[dict[str, Any]],
        max_tokens: int = 4096,
        tools: Optional[list[dict]] = None,
        tool_choice: Optional[dict] = None,
        extra_body: Optional[dict[str, Any]] = None,
    ) -> "LLMResponse":
        """
        LLM API call with raw message list and structured response.

        Parameters
        ----------
        messages    : OpenAI-format message list
        max_tokens  : maximum output tokens
        tools       : optional tool definitions forwarded to the API as-is
        tool_choice : optional tool choice forcing (e.g., {"type": "function", "function": {"name": "submit_action"}})
        extra_body  : optional extra parameters for provider-specific features (e.g., thinking disable)

        Returns
        -------
        LLMResponse : structured response with content, tool_calls, finish_reason

        Raises
        ------
        LLMError               : after max_retries failed attempts
        LLMAuthenticationError : immediately on authentication failure (not retried)
        """
        last_exc: Optional[BaseException] = None

        for attempt in range(self._max_retries):
            try:
                kwargs: dict[str, Any] = dict(
                    model=self._model,
                    max_tokens=max_tokens,
                    messages=messages,
                    timeout=REQUEST_TIMEOUT_SECONDS,
                )
                if tools is not None:
                    kwargs["tools"] = tools
                if tool_choice is not None:
                    kwargs["tool_choice"] = tool_choice
                if extra_body is not None:
                    kwargs["extra_body"] = extra_body

                response = self._client.chat.completions.create(**kwargs)
                choice = response.choices[0]
                msg = choice.message

                # Extract content - NEVER use reasoning_content as content fallback
                # reasoning_content is for internal thought, not output
                content = msg.content or ""
                raw = msg.model_dump()
                # Do NOT fall back to reasoning_content - it's not valid output

                # Check for empty content (rate limit or API hiccup) - retry if not last attempt
                if not content.strip() and not msg.tool_calls:
                    if attempt < self._max_retries - 1:
                        logger.warning(
                            "Empty response on attempt %d/%d, waiting %ds before retry",
                            attempt + 1,
                            self._max_retries,
                            RATE_LIMIT_RETRY_SECONDS,
                        )
                        time.sleep(RATE_LIMIT_RETRY_SECONDS)
                        continue
                    # Last attempt - return empty and let caller handle

                # Extract tool calls if present
                tool_calls = None
                if hasattr(msg, "tool_calls") and msg.tool_calls:
                    tool_calls = [
                        {
                            "id": tc.id,
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        }
                        for tc in msg.tool_calls
                    ]

                return LLMResponse(
                    content=content,
                    tool_calls=tool_calls,
                    finish_reason=choice.finish_reason or "stop",
                    reasoning_content=raw.get("reasoning_content") or raw.get("reasoning"),
                )

            except Exception as exc:
                exc_type = type(exc).__name__
                if exc_type in ("AuthenticationError", "PermissionDeniedError"):
                    raise LLMAuthenticationError(exc, self._model) from exc

                # Check for rate limiting (429 or RateLimitError)
                is_rate_limit = exc_type in ("RateLimitError",)
                status_code = getattr(exc, "status_code", None) or getattr(exc, "http_status", None)
                if status_code == 429:
                    is_rate_limit = True
                # Also check error message for rate limit indicators
                exc_str = str(exc).lower()
                if "rate limit" in exc_str or "429" in exc_str or "too many requests" in exc_str:
                    is_rate_limit = True

                last_exc = exc

                if is_rate_limit and attempt < self._max_retries - 1:
                    logger.warning(
                        "Rate limit hit on attempt %d/%d, waiting %ds before retry: %s",
                        attempt + 1,
                        self._max_retries,
                        RATE_LIMIT_RETRY_SECONDS,
                        exc,
                    )
                    time.sleep(RATE_LIMIT_RETRY_SECONDS)
                else:
                    logger.warning(
                        "LLM call attempt %d/%d failed (%s): %s",
                        attempt + 1,
                        self._max_retries,
                        exc_type,
                        exc,
                    )
                    if attempt < self._max_retries - 1:
                        time.sleep(self._base_delay * (2**attempt))

        raise LLMError(last_exc or Exception("Unknown error"), self._max_retries, self._model)


class LLMResponse:
    """
    Structured response from an LLM call.

    Attributes
    ----------
    content          : text content of the response (may be empty if tool_calls present)
    tool_calls       : list of tool calls requested by the model, or None
    finish_reason    : why the model stopped ("stop", "length", "tool_calls", etc.)
    reasoning_content: reasoning/thinking content from models like Moonshot (optional)
    """

    def __init__(
        self,
        content: str,
        tool_calls: Optional[list[dict]] = None,
        finish_reason: str = "stop",
        reasoning_content: Optional[str] = None,
    ) -> None:
        self.content = content
        self.tool_calls = tool_calls
        self.finish_reason = finish_reason
        self.reasoning_content = reasoning_content

    def __repr__(self) -> str:
        return (
            f"LLMResponse(content={self.content[:50]!r}..., "
            f"tool_calls={self.tool_calls is not None}, "
            f"finish_reason={self.finish_reason!r})"
        )


class LLMAuthenticationError(LLMError):
    """Raised immediately (no retry) when the API key is rejected."""

    def __init__(self, cause: BaseException, model: str) -> None:
        super().__init__(cause, attempt_count=1, model=model)
        self.__doc__ = "Authentication failed — check API key."
