"""MnemisMemoryAdapter contract tests (MEMORY_CONSISTENCY §9.2, §10).

The adapter talks to a Mnemis-compatible HTTP service over an injectable
transport so every behavior (requests, timeouts, result shaping, health) is
verifiable without a live external service.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.memory.mnemis_backend import (
    MnemisMemoryAdapter,
    MnemisMemoryResult,
    MnemisUnavailableError,
)


class RecordingTransport:
    def __init__(self, responses: dict[str, Any] | None = None, *, fail: bool = False) -> None:
        self.calls: list[tuple[str, str, dict, int]] = []
        self.responses: dict[str, Any] = responses or {}
        self.fail = fail

    async def request(
        self, method: str, path: str, body: dict, timeout_ms: int
    ) -> dict:
        self.calls.append((method, path, body, timeout_ms))
        if self.fail:
            raise TimeoutError("mnemis timed out")
        relative = path.split("//", 1)[-1].split("/", 1)
        key = (method, "/" + relative[1]) if len(relative) > 1 else (method, "/")
        if key not in self.responses:
            raise RuntimeError(f"no stub response for {key}")
        return self.responses[key]


def _run(coro) -> Any:
    return asyncio.run(coro)


def _episode_payload() -> dict:
    return {
        "episode_id": "ep_1",
        "student_id": "stu_1",
        "session_id": "ses-1",
        "skill": "linear_equations",
        "misconception": "sign_error",
        "intervention": "SHOW_WORKED_EXAMPLE",
        "outcome": {"correct": True, "hint_level": 0, "different_item": True},
        "effectiveness": 1.0,
        "evidence_event_ids": ["evt_1"],
        "summary": "worked example resolved sign_error",
        "confidence": 1.0,
        "status": "validated",
    }


def test_upsert_episode_sends_expected_request() -> None:
    transport = RecordingTransport(responses={("POST", "/index/upsert"): {"ok": True}})
    adapter = MnemisMemoryAdapter(transport=transport, base_url="http://mnemis:8000")
    _run(
        adapter.upsert_episode(
            _episode_payload(),
            idempotency_key="memory-index:stu_1:episode:ep_1:1:upsert_episode",
        )
    )
    method, path, body, timeout_ms = transport.calls[0]
    assert method == "POST"
    assert path == "http://mnemis:8000/index/upsert"
    assert body["type"] == "episode"
    assert body["idempotency_key"] == "memory-index:stu_1:episode:ep_1:1:upsert_episode"
    assert body["payload"]["episode_id"] == "ep_1"
    assert timeout_ms == 800


def test_recall_similar_shapes_results() -> None:
    transport = RecordingTransport(
        responses={
            (
                "POST",
                "/recall/similar",
            ): {
                "results": [
                    {
                        "memory_id": "mem_1",
                        "memory_type": "episode",
                        "supporting_episode_ids": ["ep_1", "ep_2"],
                        "confidence": 0.8,
                        "retrieval_score": 0.91,
                    }
                ]
            }
        }
    )
    adapter = MnemisMemoryAdapter(transport=transport)
    results = _run(adapter.recall_similar({"skill": "linear_equations"}))
    assert results == [
        MnemisMemoryResult(
            memory_id="mem_1",
            memory_type="episode",
            supporting_episode_ids=["ep_1", "ep_2"],
            confidence=0.8,
            retrieval_route="mnemis_system1",
            retrieval_score=0.91,
            index_version="mnemis-0.1.0",
        )
    ]


def test_recall_similar_excludes_results_without_evidence() -> None:
    transport = RecordingTransport(
        responses={
            (
                "POST",
                "/recall/similar",
            ): {
                "results": [
                    {
                        "memory_id": "mem_1",
                        "memory_type": "episode",
                        "supporting_episode_ids": [],
                        "confidence": 0.9,
                        "retrieval_score": 0.95,
                    },
                    {
                        "memory_id": "mem_2",
                        "memory_type": "fact",
                        "supporting_episode_ids": ["ep_9"],
                        "confidence": 0.7,
                        "retrieval_score": 0.8,
                    },
                ]
            }
        }
    )
    adapter = MnemisMemoryAdapter(transport=transport)
    results = _run(adapter.recall_similar({"skill": "linear_equations"}))
    assert [r.memory_id for r in results] == ["mem_2"]


def test_recall_similar_applies_min_confidence() -> None:
    transport = RecordingTransport(
        responses={
            (
                "POST",
                "/recall/similar",
            ): {
                "results": [
                    {
                        "memory_id": "mem_low",
                        "memory_type": "episode",
                        "supporting_episode_ids": ["ep_1"],
                        "confidence": 0.3,
                        "retrieval_score": 0.5,
                    }
                ]
            }
        }
    )
    adapter = MnemisMemoryAdapter(transport=transport, min_confidence=0.55)
    assert _run(adapter.recall_similar({"skill": "x"})) == []


def test_timeout_raises_unavailable_error() -> None:
    transport = RecordingTransport(fail=True)
    adapter = MnemisMemoryAdapter(transport=transport)
    with pytest.raises(MnemisUnavailableError):
        _run(adapter.recall_similar({"skill": "x"}))


def test_health_false_when_transport_unavailable() -> None:
    adapter = MnemisMemoryAdapter(transport=None)
    assert _run(adapter.health()) is False


def test_health_true_when_service_ok() -> None:
    transport = RecordingTransport(responses={("GET", "/health"): {"status": "ok"}})
    adapter = MnemisMemoryAdapter(transport=transport)
    assert _run(adapter.health()) is True


def test_global_select_uses_system2_timeout() -> None:
    transport = RecordingTransport(responses={("POST", "/recall/global"): {"results": []}})
    adapter = MnemisMemoryAdapter(transport=transport)
    _run(adapter.global_select({"query": "bottleneck"}, timeout_ms=3000))
    assert transport.calls[0][3] == 3000


def test_delete_student_sends_request() -> None:
    transport = RecordingTransport(
        responses={("POST", "/student/delete"): {"deleted": True}}
    )
    adapter = MnemisMemoryAdapter(transport=transport, base_url="http://mnemis:8000")
    _run(adapter.delete_student("stu_1", idempotency_key="memory-index:stu_1:student:stu_1:1:delete_student"))
    method, path, body, _ = transport.calls[0]
    assert method == "POST"
    assert path == "http://mnemis:8000/student/delete"
    assert body == {"student_id": "stu_1", "idempotency_key": "memory-index:stu_1:student:stu_1:1:delete_student"}


def test_upsert_without_transport_raises() -> None:
    adapter = MnemisMemoryAdapter(transport=None)
    with pytest.raises(MnemisUnavailableError):
        _run(adapter.upsert_episode(_episode_payload(), idempotency_key="k"))
