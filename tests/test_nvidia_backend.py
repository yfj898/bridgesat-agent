"""NvidiaMemoryIndex contract tests over authoritative PostgreSQL memory.

The index is a Mnemis-compatible derived layer. It reads validated episodes
and evidenced facts from PostgreSQL, uses an injected LLM for summaries and
reranking, and never creates or deletes authoritative rows.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import psycopg
import pytest

from app.domain.events import LearningEvent, LearningEventType
from app.infrastructure import pg
from app.infrastructure.learner_store import LearnerStore
from app.infrastructure.migration_runner import migrate_database
from app.memory.episode_builder import EpisodeBuilder
from app.memory.fallback_backend import FallbackStudentMemory
from app.memory.mnemis_backend import MnemisMemoryAdapter, MnemisUnavailableError
from app.memory.nvidia_backend import NvidiaMemoryIndex
from app.memory.pg_memory import PGMemory
from tests.pg_test_helpers import cleanup_tenant, unique_tenant_id


@pytest.fixture()
def env() -> tuple[psycopg.Connection, str]:
    admin = pg.connect_admin()
    migrate_database(admin)
    admin.close()
    tenant_id = unique_tenant_id("task3_nvidia")
    connection = pg.connect()
    connection.execute(
        "SELECT set_config('app.tenant_id', %s, false)",
        (tenant_id,),
    )
    connection.commit()
    learner = LearnerStore(connection)
    student_id, _ = learner.create_student("Ari", 20, 1200)
    try:
        yield connection, student_id
    finally:
        connection.rollback()
        connection.close()
        cleanup = pg.connect_admin()
        try:
            cleanup_tenant(cleanup, tenant_id)
        finally:
            cleanup.close()


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


def _event(session_id: str, event_id: str, student_id: str) -> LearningEvent:
    return LearningEvent(
        event_id=event_id,
        student_id=student_id,
        session_id=session_id,
        event_type=LearningEventType.ANSWER_EVALUATED,
        payload={},
        occurred_at="2026-08-07T10:00:00+00:00",
        received_at="2026-08-07T10:00:00+00:00",
    )


def _seed_episode(
    connection: psycopg.Connection,
    student_id: str,
    episode_id: str,
    *,
    skill: str = "linear_equations",
    misconception: str = "sign_error",
    session_id: str | None = None,
) -> Any:
    session_id = session_id or f"ses-{episode_id}"
    builder = EpisodeBuilder(connection)
    episode = builder.build_candidate(
        student_id=student_id,
        session_id=session_id,
        skill=skill,
        misconception=misconception,
        intervention="SHOW_WORKED_EXAMPLE",
        context_event=_event(session_id, f"ctx-{episode_id}", student_id),
        evidence_events=[_event(session_id, f"obs-{episode_id}", student_id)],
        outcome_event=_event(session_id, f"out-{episode_id}", student_id),
        outcome_correct=True,
        outcome_hint_level=0,
        outcome_content_id=f"out-{episode_id}",
        teaching_content_id=f"teach-{episode_id}",
        summary=f"summary of {episode_id}",
        episode_id=episode_id,
    )
    return builder.validate(episode)


def _index(connection: psycopg.Connection, llm: Any | None = None, **kwargs: Any) -> Any:
    return NvidiaMemoryIndex(connection, llm=llm, **kwargs)


def _call(index: Any, method: str, path: str, body: dict, timeout_ms: int = 800) -> dict:
    return asyncio.run(index.request(method, path, body, timeout_ms))


def _upsert_episode(index: Any, episode: Any, key: str) -> None:
    _call(
        index,
        "POST",
        "/index/upsert",
        {
            "type": "episode",
            "payload": episode.model_dump(),
            "idempotency_key": key,
        },
    )


class QueryErrorIndex(NvidiaMemoryIndex):
    def _select_candidates_in_transaction(self, **kwargs: Any) -> list[dict]:
        self.connection.execute(
            "SELECT * FROM task3_nvidia_query_error_regression"
        )
        return []


def test_upsert_stores_idempotently(env) -> None:
    connection, student_id = env
    episode = _seed_episode(connection, student_id, "ep_1")
    index = _index(connection)
    body = {
        "type": "episode",
        "payload": episode.model_dump(),
        "idempotency_key": "memory-index:stu_1:episode:ep_1:1:upsert_episode",
    }
    assert _call(index, "POST", "/index/upsert", body) == {"ok": True}
    assert _call(index, "POST", "/index/upsert", body) == {"ok": True}
    assert index._count_rows() == 1
    assert connection.execute(
        "SELECT COUNT(*) AS count FROM learning_episodes WHERE episode_id = %s",
        (episode.episode_id,),
    ).fetchone()["count"] == 1


def test_upsert_generates_llm_summary(env) -> None:
    connection, student_id = env
    episode = _seed_episode(connection, student_id, "ep_1")
    llm = StubLLM("distilled summary via llm")
    index = _index(connection, llm=llm)
    _upsert_episode(index, episode, "k1")
    assert llm.calls, "LLM should be asked to summarize the episode"
    assert index._get_row("k1")["summary"] == "distilled summary via llm"


def test_upsert_without_llm_keeps_payload_summary(env) -> None:
    connection, student_id = env
    episode = _seed_episode(connection, student_id, "ep_1")
    index = _index(connection, llm=None)
    _upsert_episode(index, episode, "k1")
    assert index._get_row("k1")["summary"] == "summary of ep_1"


def test_upsert_llm_failure_degrades_not_fails(env) -> None:
    connection, student_id = env
    episode = _seed_episode(connection, student_id, "ep_1")
    index = _index(connection, llm=StubLLM("x", fail=True))
    assert _call(
        index,
        "POST",
        "/index/upsert",
        {"type": "episode", "payload": episode.model_dump(), "idempotency_key": "k1"},
    ) == {"ok": True}
    assert index._count_rows() == 1


def test_recall_reranks_authoritative_candidates_with_llm(env) -> None:
    connection, student_id = env
    episode_1 = _seed_episode(connection, student_id, "ep_1")
    episode_2 = _seed_episode(connection, student_id, "ep_2")
    llm = StubLLM(
        json.dumps(
            [
                {"memory_id": "ep_2", "confidence": 0.9, "retrieval_score": 0.95},
                {"memory_id": "ep_1", "confidence": 0.6, "retrieval_score": 0.7},
            ]
        )
    )
    index = _index(connection, llm=llm)
    _upsert_episode(index, episode_1, "k-ep-1")
    _upsert_episode(index, episode_2, "k-ep-2")
    response = _call(
        index,
        "POST",
        "/recall/similar",
        {
            "query": {"student_id": student_id, "skill": "linear_equations"},
            "top_k": 5,
            "min_confidence": 0.55,
        },
    )
    results = response["results"]
    assert [result["memory_id"] for result in results] == ["ep_2", "ep_1"]
    assert [result["supporting_episode_ids"] for result in results] == [
        ["ep_2"],
        ["ep_1"],
    ]
    assert llm.calls, "LLM should be asked to rerank candidates"


def test_recall_reads_authoritative_fact_rows(env) -> None:
    connection, student_id = env
    episode = _seed_episode(connection, student_id, "ep_fact")
    fact = PGMemory(connection).upsert_fact_for_episode(episode)
    llm = StubLLM(
        json.dumps(
            [{"memory_id": fact.fact_id, "confidence": 0.9, "retrieval_score": 0.95}]
        )
    )
    index = _index(connection, llm=llm)
    _call(
        index,
        "POST",
        "/index/upsert",
        {
            "type": "fact",
            "payload": fact.model_dump(),
            "idempotency_key": "k-fact",
        },
    )
    response = _call(
        index,
        "POST",
        "/recall/similar",
        {
            "query": {
                "student_id": student_id,
                "skill": "linear_equations",
                "misconception": "sign_error",
            }
        },
    )
    assert response["results"][0]["memory_id"] == fact.fact_id
    assert response["results"][0]["supporting_episode_ids"] == [episode.episode_id]


def test_recall_uses_authoritative_pg_text_without_derived_overlay(env) -> None:
    connection, student_id = env
    episode = _seed_episode(connection, student_id, "ep_pg_text")
    llm = StubLLM(
        json.dumps(
            [{"memory_id": episode.episode_id, "confidence": 0.9, "retrieval_score": 0.9}]
        )
    )
    index = _index(connection, llm=llm)

    _call(
        index,
        "POST",
        "/recall/similar",
        {"query": {"student_id": student_id, "skill": "linear_equations"}},
    )

    assert "summary of ep_pg_text" in llm.calls[-1]


def test_episode_misconception_filter_applies_before_candidate_limit(env) -> None:
    connection, student_id = env
    matching = _seed_episode(
        connection, student_id, "ep_matching", misconception="target_error"
    )
    time.sleep(0.01)
    _seed_episode(connection, student_id, "ep_newer_wrong", misconception="other_error")
    index = _index(connection, candidate_pool=1)

    candidates = index._select_candidates(
        student_id=student_id,
        skill="linear_equations",
        misconception="target_error",
    )

    episode_ids = [
        candidate["memory_id"]
        for candidate in candidates
        if candidate["memory_type"] == "episode"
    ]
    assert episode_ids == [matching.episode_id]


def test_fact_skill_and_misconception_filters_apply_before_candidate_limit(env) -> None:
    connection, student_id = env
    target_episode = _seed_episode(
        connection,
        student_id,
        "ep_target_fact",
        skill="ratios_percentages",
        misconception="unit_rate_error",
    )
    target_fact = PGMemory(connection).upsert_fact_for_episode(target_episode)
    time.sleep(0.01)
    wrong_episode = _seed_episode(
        connection,
        student_id,
        "ep_newer_wrong_fact",
        skill="functions_models",
        misconception="slope_error",
    )
    PGMemory(connection).upsert_fact_for_episode(wrong_episode)
    index = _index(connection, candidate_pool=1)

    candidates = index._select_candidates(
        student_id=student_id,
        skill="ratios_percentages",
        misconception="unit_rate_error",
    )

    fact_ids = [
        candidate["memory_id"]
        for candidate in candidates
        if candidate["memory_type"] == "fact"
    ]
    assert fact_ids == [target_fact.fact_id]


def test_nvidia_query_failure_rolls_back_savepoint_for_pg_fallback(env) -> None:
    connection, student_id = env
    episode = _seed_episode(connection, student_id, "ep_query_error")
    llm = StubLLM(
        json.dumps(
            [{"memory_id": episode.episode_id, "confidence": 0.9, "retrieval_score": 0.9}]
        )
    )
    index = QueryErrorIndex(connection, llm=llm)
    adapter = MnemisMemoryAdapter(base_url="http://local/nvidia", transport=index)
    memory = FallbackStudentMemory(connection, mnemis=adapter)

    result = asyncio.run(
        memory.recall_similar(
            student_id=student_id,
            skill="linear_equations",
            misconception="sign_error",
        )
    )

    assert result.route == "pg"
    assert [hit.episode_id for hit in result.hits] == [episode.episode_id]
    assert connection.execute("SELECT 1 AS usable").fetchone()["usable"] == 1


def test_derived_cache_isolation_scopes_key_and_delete_to_tenant(env) -> None:
    connection, student_id = env
    episode = _seed_episode(connection, student_id, "ep_tenant_a")
    index = _index(connection, llm=StubLLM("tenant-a summary"))
    _upsert_episode(index, episode, "shared-key")
    tenant_a = connection.execute(
        "SELECT current_setting('app.tenant_id') AS tenant_id"
    ).fetchone()["tenant_id"]

    tenant_b = f"tenant_task3_cache_b_{student_id}"
    connection.execute(
        "SELECT set_config('app.tenant_id', %s, false)",
        (tenant_b,),
    )
    connection.commit()
    _call(
        index,
        "POST",
        "/index/upsert",
        {
            "type": "episode",
            "payload": {
                "episode_id": "ep_tenant_b",
                "student_id": "stu_tenant_b",
                "skill": "linear_equations",
                "summary": "tenant-b payload",
            },
            "idempotency_key": "shared-key",
        },
    )
    assert index._count_rows() == 1
    assert index._get_row("shared-key")["tenant_id"] == tenant_b

    _call(
        index,
        "POST",
        "/student/delete",
        {"student_id": "stu_tenant_b", "idempotency_key": "delete-b"},
    )
    connection.execute(
        "SELECT set_config('app.tenant_id', %s, false)",
        (tenant_a,),
    )
    connection.commit()
    assert index._count_rows() == 1
    assert index._get_row("shared-key")["tenant_id"] == tenant_a
    assert index._get_row("shared-key")["student_id"] == student_id


def test_recall_llm_failure_raises_unavailable(env) -> None:
    connection, student_id = env
    episode = _seed_episode(connection, student_id, "ep_1")
    index = _index(connection, llm=StubLLM("x", fail=True))
    _upsert_episode(index, episode, "k1")
    with pytest.raises(MnemisUnavailableError):
        _call(
            index,
            "POST",
            "/recall/similar",
            {"query": {"student_id": student_id, "skill": "linear_equations"}},
        )


def test_recall_empty_store_returns_no_results(env) -> None:
    connection, student_id = env
    llm = StubLLM("unused")
    index = _index(connection, llm=llm)
    response = _call(
        index,
        "POST",
        "/recall/similar",
        {"query": {"student_id": student_id, "skill": "linear_equations"}},
    )
    assert response == {"results": []}
    assert not llm.calls


def test_recall_respects_student_isolation_and_top_k(env) -> None:
    connection, student_id = env
    learner = LearnerStore(connection)
    other_student_id, _ = learner.create_student("Bea", 20, 1200)
    episode = _seed_episode(connection, student_id, "ep_1")
    other_episode = _seed_episode(connection, other_student_id, "ep_other")
    llm = StubLLM(
        json.dumps([{"memory_id": "ep_1", "confidence": 0.8, "retrieval_score": 0.9}])
    )
    index = _index(connection, llm=llm)
    _upsert_episode(index, episode, "k1")
    _upsert_episode(index, other_episode, "k2")
    response = _call(
        index,
        "POST",
        "/recall/similar",
        {"query": {"student_id": student_id, "skill": "linear_equations"}, "top_k": 5},
    )
    assert [result["memory_id"] for result in response["results"]] == ["ep_1"]


def test_delete_removes_only_derived_state(env) -> None:
    connection, student_id = env
    episode_1 = _seed_episode(connection, student_id, "ep_1")
    episode_2 = _seed_episode(connection, student_id, "ep_2")
    index = _index(connection, llm=StubLLM("x"))
    _upsert_episode(index, episode_1, "k-ep-1")
    _upsert_episode(index, episode_2, "k-ep-2")
    response = _call(
        index,
        "POST",
        "/student/delete",
        {"student_id": student_id, "idempotency_key": "d1"},
    )
    assert response == {"deleted": True}
    assert index._count_rows() == 0
    assert connection.execute(
        "SELECT COUNT(*) AS count FROM learning_episodes WHERE student_id = %s",
        (student_id,),
    ).fetchone()["count"] == 2


def test_health_depends_on_llm(env) -> None:
    connection, _ = env
    assert _call(_index(connection, llm=StubLLM("x")), "GET", "/health", {}) == {
        "status": "ok"
    }
    assert _call(_index(connection, llm=None), "GET", "/health", {}) == {
        "status": "degraded"
    }


def test_recall_unparseable_rerank_raises_unavailable(env) -> None:
    connection, student_id = env
    episode = _seed_episode(connection, student_id, "ep_1")
    index = _index(connection, llm=StubLLM("this is not json"))
    _upsert_episode(index, episode, "k1")
    with pytest.raises(MnemisUnavailableError):
        _call(
            index,
            "POST",
            "/recall/similar",
            {"query": {"student_id": student_id, "skill": "linear_equations"}},
        )
