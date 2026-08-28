"""Claude client used for resume parsing, match screening, and tailoring."""
from __future__ import annotations

import logging
import os
import time
from typing import Optional

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-sonnet-5"


class LLMError(RuntimeError):
    pass


class LLM:
    """Thin wrapper over the Anthropic Messages API with retry and a call budget."""

    def __init__(self, api_key: str = "", model: str = DEFAULT_MODEL,
                 max_calls: int = 400, timeout: float = 120.0):
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self.model = model or DEFAULT_MODEL
        self.max_calls = max_calls
        self.timeout = timeout
        self.calls = 0
        self._client = None

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def _get_client(self):
        if self._client is None:
            try:
                from anthropic import Anthropic
            except ImportError as exc:
                raise LLMError("pip install anthropic") from exc
            if not self.api_key:
                raise LLMError("ANTHROPIC_API_KEY is not set")
            self._client = Anthropic(api_key=self.api_key, timeout=self.timeout)
        return self._client

    def complete(self, prompt: str, max_tokens: int = 1500, system: str = "",
                 temperature: float = 0.3, retries: int = 3) -> str:
        if self.calls >= self.max_calls:
            raise LLMError(f"LLM call budget exhausted ({self.max_calls})")

        client = self._get_client()
        kwargs = {
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            kwargs["system"] = system

        last_error: Optional[Exception] = None
        for attempt in range(retries):
            try:
                response = client.messages.create(**kwargs)
                self.calls += 1
                return "".join(
                    block.text for block in response.content
                    if getattr(block, "type", "") == "text"
                ).strip()
            except Exception as exc:  # noqa: BLE001 - SDK raises several types
                last_error = exc
                message = str(exc).lower()
                # Don't burn retries on errors that will never succeed.
                if any(t in message for t in ("authentication", "api key", "permission",
                                              "not_found_error", "invalid_request")):
                    break
                wait = 2 ** attempt
                log.warning("LLM call failed (%s), retrying in %ds", exc, wait)
                time.sleep(wait)
        raise LLMError(f"LLM call failed after {retries} attempts: {last_error}")


class NullLLM(LLM):
    """Stand-in used when no API key is configured.

    Everything still runs - keyword matching, discovery, the digest, template
    cover letters - just without the AI-written parts.
    """

    def __init__(self):
        super().__init__(api_key="")

    @property
    def available(self) -> bool:
        return False

    def complete(self, *args, **kwargs) -> str:
        raise LLMError("no ANTHROPIC_API_KEY configured")


def build_llm(config) -> LLM:
    key = config.anthropic_api_key
    if not key:
        log.warning(
            "No ANTHROPIC_API_KEY - running without AI screening or tailored cover "
            "letters. Keyword matching and discovery still work."
        )
        return NullLLM()
    return LLM(api_key=key, model=config.llm_model,
               max_calls=int(config.llm.get("max_calls", 400)))
