"""Consistency metrics tests (MEMORY_CONSISTENCY §13).

Aggregates outbox health, PostgreSQL episode count, indexed episode count,
deletion pending count, index success rate and fallback rate into one
monitoring snapshot.
"""

from __future__ import annotations

import pytest

from app.infrastructure import pg
from app.infrastructure.learner_store import LearnerStore
from app.infrastructure.migration_runner import migrate_database
from app.memory.episode_builder import EpisodeBuilder
from app.memory.metrics import memory_consistency_metrics
from app.memory.mnemis_stub import InMemoryMnemisIndex
from app.memory.worker import OutboxWorker
from tests.test_memory_outbox_wiring import _event as make_event


@pytest.fixture()
def conn():
    admin = pg.connect_admin()
    migrate_database(admin)
    admin.close()
    connection = pg.connect()
    connection.execute(
        "SELECT set_config('app.tenant_id', %s, false)",
        ("tenant_test",),
    )
    connection.commit()
    yield connection
    connection.close()
    cleanup = pg.connect_admin()
    try:
        cleanup.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
        cleanup.commit()
    finally:
        cleanup.close()


def _validated_episode(connection, student_id: str) -> None:
    builder = EpisodeBuilder(connection)
    episode = builder.build_candidate(
        student_id=student_id,
        session_id="ses-1",
        skill="linear_equations",
        misconception="sign_error",
        intervention="SHOW_WORKED_EXAMPLE",
        context_event=make_event("ses-1", "ctx", student_id),
        evidence_events=[make_event("ses-1", "obs", student_id)],
        outcome_event=make_event("ses-1", "out", student_id),
        outcome_correct=True,
        outcome_hint_level=0,
        outcome_content_id="transfer",
        teaching_content_id="taught",
        summary="x",
    )
    builder.validate(episode)


def test_fresh_database_reports_zeroes(conn) -> None:
    metrics = memory_consistency_metrics(conn)
    assert metrics["outbox_pending_count"] == 0
    assert metrics["outbox_dead_letter_count"] == 0
    assert metrics["sqlite_episode_count"] == 0
    assert metrics["deletion_pending_count"] == 0
    assert metrics["memory_fallback_rate"] is None


def test_metrics_reflect_episodes_and_index(conn) -> None:
    learner = LearnerStore(conn)
    student_id, _ = learner.create_student("Ari", 20, 1200)
    _validated_episode(conn, student_id)
    index = InMemoryMnemisIndex()
    OutboxWorker(conn, index=index).run_pending()

    metrics = memory_consistency_metrics(conn, index=index)
    assert metrics["sqlite_episode_count"] == 1
    assert metrics["indexed_episode_count"] == 1
    assert metrics["memory_index_success_rate"] == 1.0
    assert metrics["outbox_pending_count"] == 0
    assert metrics["outbox_dead_letter_count"] == 0


def test_index_outage_affects_success_rate(conn) -> None:
    learner = LearnerStore(conn)
    student_id, _ = learner.create_student("Ari", 20, 1200)
    _validated_episode(conn, student_id)

    class BrokenIndex:
        async def upsert_episode(self, payload, idempotency_key):
            raise RuntimeError("down")

        async def upsert_fact(self, payload, idempotency_key):
            raise RuntimeError("down")

    worker = OutboxWorker(conn, index=BrokenIndex())
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    for _ in range(6):
        now = now + timedelta(seconds=2000)
        worker.run_pending(now=now.isoformat())

    metrics = memory_consistency_metrics(conn, index=InMemoryMnemisIndex())
    assert metrics["sqlite_episode_count"] == 1
    assert metrics["indexed_episode_count"] == 0
    assert metrics["outbox_dead_letter_count"] == 1
    assert metrics["memory_index_success_rate"] == 0.0


def test_deletion_pending_count_tracks_requests(conn) -> None:
    learner = LearnerStore(conn)
    student_id, _ = learner.create_student("Ari", 20, 1200)
    from app.memory.deletion import StudentMemoryDeletionService

    service = StudentMemoryDeletionService(conn)
    service.request_deletion(student_id)
    metrics = memory_consistency_metrics(conn)
    assert metrics["deletion_pending_count"] == 1