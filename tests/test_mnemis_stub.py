"""In-memory Mnemis-compatible index tests.

The stub implements the same async surface as MnemisMemoryAdapter so the
worker, fallback chain, parity checks and the ablation eval can run without
an external Mnemis service. Idempotency must hold: duplicate delivery creates
no duplicate memories (MEMORY_CONSISTENCY §5, §13).
"""

from __future__ import annotations

import asyncio

from app.memory.mnemis_stub import InMemoryMnemisIndex


def _run(coro):
    return asyncio.run(coro)


def _episode() -> dict:
    return {
        "episode_id": "ep_1",
        "student_id": "stu_1",
        "skill": "linear_equations",
        "misconception": "sign_error",
        "intervention": "SHOW_WORKED_EXAMPLE",
        "summary": "worked example resolved sign_error",
        "confidence": 1.0,
        "status": "validated",
    }


def test_duplicate_upsert_creates_single_memory() -> None:
    index = InMemoryMnemisIndex()
    _run(index.upsert_episode(_episode(), idempotency_key="k1"))
    _run(index.upsert_episode(_episode(), idempotency_key="k1"))
    assert _run(index.count_episodes("stu_1")) == 1
    assert index.upsert_keys_seen == {"k1"}


def test_recall_matches_skill_and_misconception() -> None:
    index = InMemoryMnemisIndex()
    _run(index.upsert_episode(_episode(), idempotency_key="k1"))
    _run(
        index.upsert_episode(
            {
                **_episode(),
                "episode_id": "ep_2",
                "skill": "ratios_percentages",
                "misconception": "ratio_inversion",
            },
            idempotency_key="k2",
        )
    )
    results = _run(
        index.recall_similar(
            {"student_id": "stu_1", "skill": "linear_equations", "misconception": "sign_error"},
            top_k=5,
        )
    )
    assert [r["memory_id"] for r in results] == ["mem:stu_1:episode:ep_1"]
    assert results[0]["supporting_episode_ids"] == ["ep_1"]
    assert results[0]["retrieval_score"] > 0.9


def test_recall_is_student_scoped() -> None:
    index = InMemoryMnemisIndex()
    _run(index.upsert_episode(_episode(), idempotency_key="k1"))
    _run(
        index.upsert_episode(
            {**_episode(), "episode_id": "ep_other", "student_id": "stu_2"},
            idempotency_key="k2",
        )
    )
    results = _run(
        index.recall_similar(
            {"student_id": "stu_1", "skill": "linear_equations", "misconception": "sign_error"}
        )
    )
    assert [r["memory_id"] for r in results] == ["mem:stu_1:episode:ep_1"]


def test_delete_student_removes_all_memories() -> None:
    index = InMemoryMnemisIndex()
    _run(index.upsert_episode(_episode(), idempotency_key="k1"))
    _run(
        index.upsert_fact(
            {"fact_id": "f1", "student_id": "stu_1", "confidence": 0.7},
            idempotency_key="k2",
        )
    )
    _run(index.delete_student("stu_1", idempotency_key="kd"))
    assert _run(index.count_episodes("stu_1")) == 0
    results = _run(index.recall_similar({"student_id": "stu_1", "skill": "linear_equations"}))
    assert results == []
    assert _run(index.count_facts("stu_1")) == 0


def test_health_true() -> None:
    index = InMemoryMnemisIndex()
    assert _run(index.health()) is True
