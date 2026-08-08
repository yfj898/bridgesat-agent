"""Minimal OpenAI-compatible LLM client (NVIDIA NIM etc.).

Speaks the ``POST /v1/chat/completions`` format over an injectable async
transport so tests never hit a live endpoint. When no API key is configured
the client is unavailable, mirroring the Mnemis degradation contract: callers
fall back to the deterministic policy instead of failing the session.
"""

from __future__ import annotations

import os
from typing import Any, Awaitable, Callable

DEFAULT_TIMEOUT_MS = int(os.getenv("BRIDGESAT_LLM_TIMEOUT_MS", "8000"))

Transport = Callable[..., Awaitable[dict]]


def _env_defaults() -> dict:
    return {
        "base_url": os.getenv(
            "BRIDGESAT_LLM_BASE_URL", "https://integrate.api.nvidia.com/v1"
        ),
        "api_key": os.getenv("BRIDGESAT_LLM_API_KEY", ""),
        "model": os.getenv("BRIDGESAT_LLM_MODEL", "deepseek-ai/deepseek-v4-flash-0731"),
    }


class LLMUnavailableError(RuntimeError):
    """LLM timed out, errored, or is not configured."""


def _default_transport(api_key: str, base_url: str):
    """httpx-backed OpenAI-compatible transport used when none is injected."""

    async def transport(url: str, body: dict, timeout_ms: int) -> dict:
        import httpx

        if not api_key:
            raise LLMUnavailableError("LLM API key is not configured")
        headers = {"Authorization": f"Bearer {api_key}"}
        try:
            async with httpx.AsyncClient(timeout=timeout_ms / 1000) as client:
                response = await client.post(url, json=body, headers=headers)
        except httpx.TimeoutException as exc:
            raise LLMUnavailableError(f"LLM timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise LLMUnavailableError(f"LLM request failed: {exc}") from exc
        if response.status_code != 200:
            raise LLMUnavailableError(f"LLM returned status {response.status_code}")
        try:
            return response.json()
        except ValueError as exc:
            raise LLMUnavailableError("LLM returned a non-JSON response") from exc

    return transport


class LLMClient:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        transport: Transport | None = None,
        timeout_ms: int | None = None,
    ) -> None:
        self.api_key = api_key if api_key is not None else _env_defaults()["api_key"]
        self.base_url = (base_url or _env_defaults()["base_url"]).rstrip("/")
        self.model = model or _env_defaults()["model"]
        self._transport = transport or _default_transport(self.api_key, self.base_url)
        self.timeout_ms = timeout_ms or DEFAULT_TIMEOUT_MS

    async def complete(
        self,
        prompt: str,
        *,
        max_tokens: int = 200,
        temperature: float = 0.0,
        timeout_ms: int | None = None,
    ) -> str:
        if not self.api_key:
            raise LLMUnavailableError("LLM API key is not configured")
        request = (
            self._transport.request
            if hasattr(self._transport, "request")
            else self._transport
        )
        try:
            response = await request(
                f"{self.base_url}/chat/completions",
                {
                    "model": self.model,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                },
                timeout_ms or self.timeout_ms,
            )
        except LLMUnavailableError:
            raise
        except Exception as exc:
            raise LLMUnavailableError(f"LLM call failed: {exc}") from exc
        try:
            content = response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMUnavailableError("LLM returned a non-chat response") from exc
        if not isinstance(content, str) or not content.strip():
            # Reasoning models may return None content when max_tokens is
            # consumed by the reasoning pass; treat as unavailable so callers
            # degrade instead of crashing.
            raise LLMUnavailableError("LLM returned empty content")
        return content
