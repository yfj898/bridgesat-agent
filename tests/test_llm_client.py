"""LLM client contract tests.

The client speaks the OpenAI-compatible chat completions format over an
injectable async transport so every behavior (request shape, timeout, error
mapping, model defaults) is verifiable without a live external service.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.agent.llm_client import LLMClient, LLMUnavailableError


class RecordingTransport:
    def __init__(self, response: dict | None = None, *, fail: bool = False) -> None:
        self.calls: list[tuple[str, dict, int]] = []
        self.response = response
        self.fail = fail

    async def request(self, url: str, body: dict, timeout_ms: int) -> dict:
        self.calls.append((url, body, timeout_ms))
        if self.fail:
            raise TimeoutError("nvidia timed out")
        if self.response is None:
            raise RuntimeError("no stub response")
        return self.response


def _run(coro) -> Any:
    return asyncio.run(coro)


def _chat_response(content: str) -> dict:
    return {
        "choices": [
            {"message": {"role": "assistant", "content": content}}
        ]
    }


def test_completion_sends_openai_compatible_request() -> None:
    transport = RecordingTransport(response=_chat_response("2"))
    client = LLMClient(
        api_key="nvapi-test",
        model="meta/llama-3.1-8b-instruct",
        base_url="https://integrate.api.nvidia.com/v1",
        transport=transport,
    )

    result = _run(
        client.complete("Solve 2x+3=7", max_tokens=50, temperature=0.0)
    )

    assert result == "2"
    url, body, timeout_ms = transport.calls[0]
    assert url == "https://integrate.api.nvidia.com/v1/chat/completions"
    assert body["model"] == "meta/llama-3.1-8b-instruct"
    assert body["messages"] == [{"role": "user", "content": "Solve 2x+3=7"}]
    assert body["max_tokens"] == 50
    assert body["temperature"] == 0.0
    assert timeout_ms == 8000


def test_defaults_from_environment(monkeypatch) -> None:
    monkeypatch.setenv("BRIDGESAT_LLM_API_KEY", "nvapi-env")
    monkeypatch.setenv("BRIDGESAT_LLM_BASE_URL", "https://env.example/v1")
    monkeypatch.setenv("BRIDGESAT_LLM_MODEL", "env/model")
    transport = RecordingTransport(response=_chat_response("ok"))
    client = LLMClient(transport=transport)

    _run(client.complete("hi", max_tokens=5))

    url, body, _ = transport.calls[0]
    assert body["model"] == "env/model"
    assert url == "https://env.example/v1/chat/completions"


def test_unavailable_when_timeout() -> None:
    transport = RecordingTransport(fail=True)
    client = LLMClient(api_key="k", model="m", transport=transport)

    with pytest.raises(LLMUnavailableError):
        _run(client.complete("hi"))


def test_unavailable_when_non_choices_response() -> None:
    transport = RecordingTransport(response={"unexpected": True})
    client = LLMClient(api_key="k", model="m", transport=transport)

    with pytest.raises(LLMUnavailableError):
        _run(client.complete("hi"))


def test_unavailable_when_empty_content() -> None:
    transport = RecordingTransport(
        response={"choices": [{"message": {"role": "assistant", "content": None}}]}
    )
    client = LLMClient(api_key="k", model="m", transport=transport)

    with pytest.raises(LLMUnavailableError):
        _run(client.complete("hi"))


def test_unavailable_when_no_api_key() -> None:
    client = LLMClient(api_key="", model="m", transport=None)

    with pytest.raises(LLMUnavailableError):
        _run(client.complete("hi"))
