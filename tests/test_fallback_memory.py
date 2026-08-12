"""FallbackStudentMemory tests against the authoritative PostgreSQL store.

Chain: Mnemis within a strict timeout -> PostgreSQL -> offline snapshot. The
PostgreSQL route must always produce memory when Mnemis is unavailable or too
slow, and fallback must be measurable.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time

import psycopg
import pytest

from app.agent.orchestrator import SessionOrchestrator
from app.domain.events import LearningEvent, LearningEventType
from app.domain.memory import Episode
from app.infrastructure import pg
from app.infrastructure.learner_store import LearnerStore
from app.infrastructure.migration_runner import migrate_database
from app.memory.episode_builder import EpisodeBuilder, utc_now_iso
from app.memory.fallback_backend import FallbackStudentMemory
from app.memory.mnemis_backend import MnemisMemoryAdapter, MnemisUnavailableError
from app.memory.mnemis_stub import InMemoryMnemisIndex
from app.memory.nvidia_backend import NvidiaMemoryIndex
from app.memory.pg_memory import PGMemory
from tests.pg_test_helpers import cleanup_tenant, unique_tenant_id


@pytest.fixture()
def db() -> tuple[psycopg.Connection, str, Episode]:
    admin = pg.connect_admin()
    migrate_database(admin)
    admin.close()

    tenant_id = unique_tenant_id("task3_fallback")
    connection = pg.connect()
    connection.execute(
        "SELECT set_config('app.tenant_id', %s, false)",
        (tenant_id,),
    )
    connection.commit()
    learner = LearnerStore(connection)
    student_id, _ = learner.create_student("Ari", 20, 1200)
    builder = EpisodeBuilder(connection)
    episode = builder.build_candidate(
        student_id=student_id,
        session_id="ses-1",
        skill="linear_equations",
        misconception="sign_error",
        intervention="SHOW_WORKED_EXAMPLE",
        context_event=_event("ses-1", "ctx", student_id),
        evidence_events=[_event("ses-1", "obs", student_id)],
        outcome_event=_event("ses-1", "out", student_id),
        outcome_correct=True,
        outcome_hint_level=0,
        outcome_content_id="transfer",
        teaching_content_id="taught",
        summary="authoritative PG episode",
        episode_id="ep_pg",
    )
    episode = builder.validate(episode)
    try:
        yield connection, student_id, episode
    finally:
        connection.rollback()
        connection.close()
        cleanup = pg.connect_admin()
        try:
            cleanup_tenant(cleanup, tenant_id)
        finally:
            cleanup.close()


def _event(session_id: str, event_id: str, student_id: str) -> LearningEvent:
    return LearningEvent(
        event_id=event_id,
        student_id=student_id,
        session_id=session_id,
        event_type=LearningEventType.ANSWER_EVALUATED,
        payload={},
        occurred_at="2026-08-06T10:00:00+00:00",
        received_at="2026-08-06T10:00:00+00:00",
    )


class FailingIndex:
    async def recall_similar(self, query, **kwargs):
        raise MnemisUnavailableError("down")

    async def health(self) -> bool:
        return False


class ErrorIndex:
    async def recall_similar(self, query, **kwargs):
        raise RuntimeError("unexpected index failure")

    async def health(self) -> bool:
        return False


class SlowIndex:
    async def recall_similar(self, query, **kwargs):
        await asyncio.sleep(0.5)
        return []

    async def health(self) -> bool:
        return True


class _RankingLLM:
    async def complete(self, prompt: str, **kwargs) -> str:
        return '[{"memory_id": "ep_pg", "confidence": 0.9, "retrieval_score": 0.95}]'


def test_foreign_mnemis_evidence_never_reaches_policy(db) -> None:
    """A Mnemis response whose supporting episode IDs are not tenant-scoped
    validated PostgreSQL episodes must be filtered before reaching policy."""
    connection, student_id, episode = db

    class ForeignIndex:
        async def recall_similar(self, query, **kwargs):
            return [
                {
                    "memory_id": "mem_foreign",
                    "memory_type": "episode",
                    "supporting_episode_ids": ["ep_evil_other_student"],
                    "confidence": 0.95,
                    "retrieval_route": "mnemis_system1",
                    "retrieval_score": 0.99,
                },
                {
                    "memory_id": "mem_legit",
                    "memory_type": "episode",
                    "supporting_episode_ids": [episode.episode_id],
                    "confidence": 0.95,
                    "retrieval_route": "mnemis_system1",
                    "retrieval_score": 0.9,
                },
            ]

    memory = FallbackStudentMemory(connection, mnemis=ForeignIndex())
    result = asyncio.run(
        memory.recall_similar(
            student_id=student_id, skill="linear_equations", misconception="sign_error"
        )
    )
    assert [r.episode_id for r in result.hits] == [episode.episode_id]
    assert all(
        r.episode_id != "ep_evil_other_student" for r in result.hits
    )


def test_all_foreign_mnemis_results_fall_back_to_pg(db) -> None:
    """When the index returns only foreign evidence, recall must not present
    an empty hit list as a successful Mnemis route; it falls back to PG."""
    connection, student_id, episode = db

    class OnlyForeignIndex:
        async def recall_similar(self, query, **kwargs):
            return [
                {
                    "memory_id": "mem_evil",
                    "memory_type": "episode",
                    "supporting_episode_ids": ["ep_other_student"],
                    "confidence": 0.99,
                    "retrieval_route": "mnemis_system1",
                    "retrieval_score": 1.0,
                }
            ]

    memory = FallbackStudentMemory(connection, mnemis=OnlyForeignIndex())
    result = asyncio.run(
        memory.recall_similar(
            student_id=student_id, skill="linear_equations", misconception="sign_error"
        )
    )
    assert result.route == "pg"
    assert [r.episode_id for r in result.hits] == [episode.episode_id]


def test_mnemis_results_take_priority(db) -> None:
    connection, student_id, episode = db
    mnemis = InMemoryMnemisIndex()
    asyncio.run(
        mnemis.upsert_episode(
            episode.model_dump(),
            idempotency_key="k1",
        )
    )
    memory = FallbackStudentMemory(connection, mnemis=mnemis)
    result = asyncio.run(
        memory.recall_similar(
            student_id=student_id, skill="linear_equations", misconception="sign_error"
        )
    )
    assert result.route == "mnemis_system1"
    assert [r.episode_id for r in result.hits] == [episode.episode_id]
    assert memory.recall_metrics()["memory_fallback_rate"] == 0.0


def test_mnemis_unavailable_falls_back_to_pg(db) -> None:
    connection, student_id, episode = db
    memory = FallbackStudentMemory(connection, mnemis=FailingIndex())
    result = asyncio.run(
        memory.recall_similar(
            student_id=student_id, skill="linear_equations", misconception="sign_error"
        )
    )
    assert result.route == "pg"
    assert [hit.episode_id for hit in result.hits] == [episode.episode_id]
    assert result.hits[0].retrieval_route == "pg"
    metrics = memory.recall_metrics()
    assert metrics["memory_fallback_rate"] == 1.0
    assert metrics["memory_route_counts"]["pg"] == 1
    assert metrics["memory_route_counts"].get("mnemis_system1", 0) == 0


def test_mnemis_runtime_error_falls_back_to_pg(db) -> None:
    connection, student_id, episode = db
    memory = FallbackStudentMemory(connection, mnemis=ErrorIndex())

    result = asyncio.run(
        memory.recall_similar(
            student_id=student_id, skill="linear_equations", misconception="sign_error"
        )
    )

    assert result.route == "pg"
    assert [hit.episode_id for hit in result.hits] == [episode.episode_id]
    assert memory.recall_metrics()["memory_route_counts"] == {"pg": 1}


def test_nvidia_index_drives_mnemis_route(db) -> None:
    """Nvidia reads the authoritative PG episode before reranking it."""
    connection, student_id, episode = db
    index = NvidiaMemoryIndex(connection, llm=_RankingLLM())
    adapter = MnemisMemoryAdapter(base_url="http://local/nvidia", transport=index)
    asyncio.run(
        adapter.upsert_episode(
            episode.model_dump(),
            idempotency_key="memory-index:k1",
        )
    )
    memory = FallbackStudentMemory(connection, mnemis=adapter)
    result = asyncio.run(
        memory.recall_similar(
            student_id=student_id, skill="linear_equations", misconception="sign_error"
        )
    )
    assert result.route == "mnemis_system1"
    assert [r.episode_id for r in result.hits] == [episode.episode_id]
    assert memory.recall_metrics()["memory_fallback_rate"] == 0.0


def test_slow_mnemis_falls_back_to_pg_within_budget(db) -> None:
    connection, student_id, episode = db
    memory = FallbackStudentMemory(connection, mnemis=SlowIndex(), timeout_ms=200)
    started = time.perf_counter()
    result = asyncio.run(
        memory.recall_similar(
            student_id=student_id, skill="linear_equations", misconception="sign_error"
        )
    )
    elapsed = time.perf_counter() - started
    assert result.route == "pg"
    assert [hit.episode_id for hit in result.hits] == [episode.episode_id]
    assert elapsed < 0.4
    assert memory.recall_metrics()["memory_fallback_rate"] == 1.0


def test_offline_snapshot_is_last_resort(db) -> None:
    connection, student_id, _ = db
    snapshot = [
        Episode(
            episode_id="snap_1",
            student_id=student_id,
            session_id="ses-snap",
            skill="linear_equations",
            misconception="sign_error",
            intervention="SHOW_WORKED_EXAMPLE",
            outcome={"correct": True, "hint_level": 0, "different_item": True},
            effectiveness=1.0,
            evidence_event_ids=["e1"],
            summary="from snapshot",
            confidence=0.8,
            status="validated",
            created_at=utc_now_iso(),
            updated_at=utc_now_iso(),
        )
    ]

    class SnapshotProvider:
        def recall_episodes(self, *, student_id, skill, misconception=None, limit=5):
            return snapshot

    memory = FallbackStudentMemory(
        connection,
        mnemis=FailingIndex(),
        offline_snapshot=SnapshotProvider(),
    )
    result = asyncio.run(
        memory.recall_similar(student_id=student_id, skill="ratios_percentages")
    )
    assert result.route == "offline_snapshot"
    assert [r.episode_id for r in result.hits] == ["snap_1"]


def test_no_mnemis_configured_uses_pg(db) -> None:
    connection, student_id, episode = db
    memory = FallbackStudentMemory(connection, mnemis=None)
    result = asyncio.run(
        memory.recall_similar(
            student_id=student_id, skill="linear_equations", misconception="sign_error"
        )
    )
    assert result.route == "pg"
    assert [hit.episode_id for hit in result.hits] == [episode.episode_id]


def test_fallback_recall_preserves_omitted_vs_null_misconception_scope(db) -> None:
    connection, student_id, episode = db
    builder = EpisodeBuilder(connection)
    generic = builder.build_candidate(
        student_id=student_id,
        session_id="ses-generic",
        skill="linear_equations",
        misconception=None,
        intervention="SHOW_MICRO_LESSON",
        context_event=_event("ses-generic", "ctx-generic", student_id),
        evidence_events=[_event("ses-generic", "obs-generic", student_id)],
        outcome_event=_event("ses-generic", "out-generic", student_id),
        outcome_correct=True,
        outcome_hint_level=0,
        outcome_content_id="generic-transfer",
        teaching_content_id="generic-lesson",
        summary="generic authoritative episode",
        episode_id="ep_generic",
    )
    builder.validate(generic)
    memory = FallbackStudentMemory(connection, mnemis=None)

    wildcard = asyncio.run(
        memory.recall_similar(student_id=student_id, skill="linear_equations")
    )
    null_scope = asyncio.run(
        memory.recall_similar(
            student_id=student_id,
            skill="linear_equations",
            misconception=None,
        )
    )

    assert {hit.episode_id for hit in wildcard.hits} == {
        episode.episode_id,
        generic.episode_id,
    }
    assert [hit.episode_id for hit in null_scope.hits] == [generic.episode_id]


def test_pg_constructors_do_not_open_sqlite(monkeypatch: pytest.MonkeyPatch, db) -> None:
    connection, _, _ = db

    def fail_sqlite(*args, **kwargs):
        raise AssertionError("SQLite constructor invoked")

    import app.agent.orchestrator as orchestrator_module
    import app.memory.fallback_backend as fallback_module

    monkeypatch.setattr(sqlite3, "connect", fail_sqlite)
    monkeypatch.setattr(
        orchestrator_module, "SQLiteMemory", fail_sqlite, raising=False
    )
    monkeypatch.setattr(fallback_module, "SQLiteMemory", fail_sqlite, raising=False)
    orchestrator = SessionOrchestrator(connection)
    fallback = FallbackStudentMemory(connection)

    assert orchestrator.connection is connection
    assert isinstance(orchestrator.memory, PGMemory)
    assert isinstance(fallback.pg, PGMemory)
