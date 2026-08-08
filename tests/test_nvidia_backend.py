"""NvidiaMemoryIndex contract tests (LLM memory recall layer).

The index is a Mnemis-transport-compatible local store: it accepts the same
``request(method, path, body, timeout_ms)`` surface so it can be dropped into
MnemisMemoryAdapter, but it persists episodes/facts in local SQLite and uses
an injected LLM for summaries and recall reranking. LLM failure degrades to
the existing fallback chain (MnemisUnavailableError), never breaks a session.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from app.memory.mnemis_backend import MnemisUnavailableError


class StubLLM:
    def __init__(self, content: str = "reranked", *, fail: bool = False) -> None:
        self.content = content
        self.fail = fail
        self.calls: list[str] = []

    async def complete(self, prompt: str, **kwargs: Any) -> str:
        self.calls.append(prompt)
        if self.fail:
            raise MnemisUnavailableError("llm down")
        return self.content


def _episode_payload(episode_id: str, student_id: str = "stu_1") -> dict:
    return {
        "episode_id": episode_id,
        "student_id": student_id,
        "session_id": f"ses-{episode_id}",
        "skill": "linear_equations",
        "misconception": "sign_error",
        "intervention": "SHOW_WORKED_EXAMPLE",
        "outcome": {"correct": True, "hint_level": 0, "different_item": True},
        "effectiveness": 1.0,
        "evidence_event_ids": ["evt_1"],
        "summary": f"summary of {episode_id}",
        "confidence": 1.0,
        "status": "validated",
    }


def _index(database_path: Path, llm: Any | None = None, **kwargs: Any) -> Any:
    from app.memory.nvidia_backend import NvidiaMemoryIndex

    return NvidiaMemoryIndex(database_path, llm=llm, **kwargs)


def _call(index: Any, method: str, path: str, body: dict, timeout_ms: int = 800) -> dict:
    return asyncio.run(index.request(method, path, body, timeout_ms))


def test_upsert_stores_idempotently(tmp_path: Path) -> None:
    index = _index(tmp_path / "mem.db")
    body = {
        "type": "episode",
        "payload": _episode_payload("ep_1"),
        "idempotency_key": "memory-index:stu_1:episode:ep_1:1:upsert_episode",
    }
    assert _call(index, "POST", "/index/upsert", body) == {"ok": True}
    assert _call(index, "POST", "/index/upsert", body) == {"ok": True}
    count = index._count_rows()
    assert count == 1


def test_upsert_generates_llm_summary(tmp_path: Path) -> None:
    llm = StubLLM("distilled summary via llm")
    index = _index(tmp_path / "mem.db", llm=llm)
    body = {
        "type": "episode",
        "payload": _episode_payload("ep_1"),
        "idempotency_key": "k1",
    }
    _call(index, "POST", "/index/upsert", body)
    assert llm.calls, "LLM should be asked to summarize the episode"
    row = index._get_row("k1")
    assert row["summary"] == "distilled summary via llm"


def test_upsert_without_llm_keeps_payload_summary(tmp_path: Path) -> None:
    index = _index(tmp_path / "mem.db", llm=None)
    body = {
        "type": "episode",
        "payload": _episode_payload("ep_1"),
        "idempotency_key": "k1",
    }
    _call(index, "POST", "/index/upsert", body)
    assert index._get_row("k1")["summary"] == "summary of ep_1"


def test_upsert_llm_failure_degrades_not_fails(tmp_path: Path) -> None:
    llm = StubLLM("x", fail=True)
    index = _index(tmp_path / "mem.db", llm=llm)
    body = {
        "type": "episode",
        "payload": _episode_payload("ep_1"),
        "idempotency_key": "k1",
    }
    assert _call(index, "POST", "/index/upsert", body) == {"ok": True}
    assert index._count_rows() == 1


def test_recall_reranks_candidates_with_llm(tmp_path: Path) -> None:
    llm = StubLLM(
        json.dumps(
            [
                {"memory_id": "ep_2", "confidence": 0.9, "retrieval_score": 0.95},
                {"memory_id": "ep_1", "confidence": 0.6, "retrieval_score": 0.7},
            ]
        )
    )
    index = _index(tmp_path / "mem.db", llm=llm)
    for ep in ("ep_1", "ep_2"):
        _call(
            index,
            "POST",
            "/index/upsert",
            {
                "type": "episode",
                "payload": _episode_payload(ep),
                "idempotency_key": f"k-{ep}",
            },
        )
    response = _call(
        index,
        "POST",
        "/recall/similar",
        {
            "query": {"student_id": "stu_1", "skill": "linear_equations"},
            "top_k": 5,
            "min_confidence": 0.55,
        },
    )
    results = response["results"]
    assert [r["memory_id"] for r in results] == ["ep_2", "ep_1"]
    assert [r["supporting_episode_ids"] for r in results] == [["ep_2"], ["ep_1"]]
    assert llm.calls, "LLM should be asked to rerank candidates"


def test_recall_llm_failure_raises_unavailable(tmp_path: Path) -> None:
    llm = StubLLM("x", fail=True)
    index = _index(tmp_path / "mem.db", llm=llm)
    _call(
        index,
        "POST",
        "/index/upsert",
        {
            "type": "episode",
            "payload": _episode_payload("ep_1"),
            "idempotency_key": "k1",
        },
    )
    with pytest.raises(MnemisUnavailableError):
        _call(
            index,
            "POST",
            "/recall/similar",
            {"query": {"student_id": "stu_1", "skill": "linear_equations"}},
        )


def test_recall_empty_store_returns_no_results(tmp_path: Path) -> None:
    llm = StubLLM("unused")
    index = _index(tmp_path / "mem.db", llm=llm)
    response = _call(
        index,
        "POST",
        "/recall/similar",
        {"query": {"student_id": "stu_1", "skill": "linear_equations"}},
    )
    assert response == {"results": []}
    assert not llm.calls


def test_recall_respects_student_isolation_and_top_k(tmp_path: Path) -> None:
    llm = StubLLM(
        json.dumps([{"memory_id": "ep_1", "confidence": 0.8, "retrieval_score": 0.9}])
    )
    index = _index(tmp_path / "mem.db", llm=llm)
    _call(
        index,
        "POST",
        "/index/upsert",
        {
            "type": "episode",
            "payload": _episode_payload("ep_1", student_id="stu_1"),
            "idempotency_key": "k1",
        },
    )
    _call(
        index,
        "POST",
        "/index/upsert",
        {
            "type": "episode",
            "payload": _episode_payload("ep_other", student_id="stu_2"),
            "idempotency_key": "k2",
        },
    )
    response = _call(
        index,
        "POST",
        "/recall/similar",
        {"query": {"student_id": "stu_1", "skill": "linear_equations"}, "top_k": 5},
    )
    assert [r["memory_id"] for r in response["results"]] == ["ep_1"]


def test_delete_student_removes_rows(tmp_path: Path) -> None:
    index = _index(tmp_path / "mem.db", llm=StubLLM("x"))
    for ep in ("ep_1", "ep_2"):
        _call(
            index,
            "POST",
            "/index/upsert",
            {
                "type": "episode",
                "payload": _episode_payload(ep),
                "idempotency_key": f"k-{ep}",
            },
        )
    response = _call(
        index, "POST", "/student/delete", {"student_id": "stu_1", "idempotency_key": "d1"}
    )
    assert response == {"deleted": True}
    assert index._count_rows() == 0


def test_health_depends_on_llm(tmp_path: Path) -> None:
    assert _call(_index(tmp_path / "a.db", llm=StubLLM("x")), "GET", "/health", {}) == {
        "status": "ok"
    }
    assert _call(_index(tmp_path / "b.db", llm=None), "GET", "/health", {}) == {
        "status": "degraded"
    }


def test_recall_unparseable_rerank_raises_unavailable(tmp_path: Path) -> None:
    llm = StubLLM("this is not json")
    index = _index(tmp_path / "mem.db", llm=llm)
    _call(
        index,
        "POST",
        "/index/upsert",
        {
            "type": "episode",
            "payload": _episode_payload("ep_1"),
            "idempotency_key": "k1",
        },
    )
    with pytest.raises(MnemisUnavailableError):
        _call(
            index,
            "POST",
            "/recall/similar",
            {"query": {"student_id": "stu_1", "skill": "linear_equations"}},
        )
