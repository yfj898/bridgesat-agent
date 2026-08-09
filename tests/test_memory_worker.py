"""In-process outbox worker tests on PostgreSQL (MEMORY_CONSISTENCY §4, §13).

pending -> processing -> indexed | retrying -> dead_letter, with stable
idempotency keys so duplicate delivery never duplicates indexed memories.
A restart must resume pending delivery; a crashed claim (stale processing
lease) must be reclaimed.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app.domain.memory import Episode
from app.infrastructure import pg
from app.infrastructure.learner_store import LearnerStore
from app.infrastructure.migration_runner import migrate_database
from app.memory.episode_builder import utc_now_iso
from app.memory.mnemis_backend import MnemisUnavailableError
from app.memory.mnemis_stub import InMemoryMnemisIndex
from app.memory.outbox import OutboxRepository
from app.memory.worker import OutboxWorker


def _episode_payload(student_id: str, episode_id: str = "ep_1") -> dict:
    return Episode(
        episode_id=episode_id,
        student_id=student_id,
        session_id="ses-1",
        skill="linear_equations",
        misconception="sign_error",
        intervention="SHOW_WORKED_EXAMPLE",
        outcome={"correct": True, "hint_level": 0, "different_item": True},
        effectiveness=1.0,
        evidence_event_ids=["evt_1"],
        summary="worked example resolved sign_error",
        confidence=1.0,
        status="validated",
        created_at=utc_now_iso(),
        updated_at=utc_now_iso(),
    ).model_dump()


@pytest.fixture()
def env() -> tuple[object, str, OutboxRepository]:
    admin = pg.connect_admin()
    migrate_database(admin)
    admin.close()
    conn = pg.connect()
    conn.execute("SELECT set_config('app.tenant_id', %s, false)", ("tenant_test",))
    conn.commit()
    learner = LearnerStore(conn)
    student_id, _ = learner.create_student("Ari", 20, 1200)
    yield conn, student_id, OutboxRepository(conn)
    conn.close()
    cleanup = pg.connect_admin()
    try:
        cleanup.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
        cleanup.commit()
    finally:
        cleanup.close()


def _enqueue(
    repo: OutboxRepository,
    student_id: str,
    *,
    operation: str = "upsert_episode",
    aggregate_type: str = "episode",
    aggregate_id: str = "ep_1",
    version: int = 1,
    payload: dict | None = None,
) -> str:
    if payload is None:
        payload = _episode_payload(student_id, aggregate_id)
    outbox_id = repo.enqueue(
        repo.connection,
        student_id=student_id,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        operation=operation,
        payload=payload,
        version=version,
    )
    repo.connection.commit()
    return outbox_id


def test_worker_indexes_pending_episodes(env: tuple[object, str, OutboxRepository]) -> None:
    connection, student_id, repo = env
    outbox_id = _enqueue(repo, student_id)
    index = InMemoryMnemisIndex()
    worker = OutboxWorker(connection, index=index)

    processed = worker.run_pending()

    assert processed == 1
    assert repo.get(outbox_id).status == "indexed"
    assert asyncio.run(index.count_episodes(student_id)) == 1
    assert index.all_episode_ids(student_id) == {"ep_1"}


def test_worker_indexes_facts(env: tuple[object, str, OutboxRepository]) -> None:
    connection, student_id, repo = env
    outbox_id = _enqueue(
        repo,
        student_id,
        operation="upsert_fact",
        aggregate_type="fact",
        aggregate_id="fact_1",
        payload={
            "fact_id": "fact_1",
            "student_id": student_id,
            "category": "misconception_intervention",
            "normalized_key": "k",
            "fact_text": "worked examples help sign errors",
            "confidence": 0.7,
            "supporting_episode_ids": ["ep_1"],
            "status": "inference",
            "version": 1,
        },
    )
    index = InMemoryMnemisIndex()
    worker = OutboxWorker(connection, index=index)
    worker.run_pending()
    assert repo.get(outbox_id).status == "indexed"
    assert asyncio.run(index.count_facts(student_id)) == 1


def test_failure_retries_then_indexes(env: tuple[object, str, OutboxRepository]) -> None:
    connection, student_id, repo = env
    outbox_id = _enqueue(repo, student_id)
    index = InMemoryMnemisIndex()

    class FlakyIndex(InMemoryMnemisIndex):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        async def upsert_episode(self, payload, idempotency_key):
            self.calls += 1
            if self.calls == 1:
                raise MnemisUnavailableError("transient")
            return await super().upsert_episode(payload, idempotency_key)

    flaky = FlakyIndex()
    worker = OutboxWorker(connection, index=flaky)
    worker.run_pending()
    assert repo.get(outbox_id).status == "retrying"
    worker.run_pending()
    assert repo.get(outbox_id).status == "indexed"
    assert asyncio.run(flaky.count_episodes(student_id)) == 1


def test_duplicate_delivery_creates_single_memory(
    env: tuple[object, str, OutboxRepository]
) -> None:
    connection, student_id, repo = env
    outbox_id = _enqueue(repo, student_id)
    index = InMemoryMnemisIndex()
    worker = OutboxWorker(connection, index=index)
    worker.run_pending()
    assert asyncio.run(index.count_episodes(student_id)) == 1
    repo.connection.execute(
        "UPDATE memory_outbox SET status = %s, next_attempt_at = %s WHERE outbox_id = %s",
        ("pending", utc_now_iso(), outbox_id),
    )
    repo.connection.commit()
    worker.run_pending()
    assert asyncio.run(index.count_episodes(student_id)) == 1


def test_repeated_failures_dead_letter(env: tuple[object, str, OutboxRepository]) -> None:
    connection, student_id, repo = env
    outbox_id = _enqueue(repo, student_id)

    class BrokenIndex:
        async def upsert_episode(self, payload, idempotency_key):
            raise MnemisUnavailableError("down")

        async def upsert_fact(self, payload, idempotency_key):
            raise MnemisUnavailableError("down")

    worker = OutboxWorker(connection, index=BrokenIndex())
    now = datetime.now(UTC)
    for _ in range(6):
        now = now + timedelta(seconds=2000)
        worker.run_pending(now=now.isoformat())
    assert repo.get(outbox_id).status == "dead_letter"
    assert repo.get(outbox_id).attempt_count == 6


def test_restart_resumes_pending_delivery(
    env: tuple[object, str, OutboxRepository]
) -> None:
    connection, student_id, repo = env
    outbox_id = _enqueue(repo, student_id)
    index = InMemoryMnemisIndex()
    first = OutboxWorker(connection, index=index)
    first.run_pending()
    assert repo.get(outbox_id).status == "indexed"

    outbox_2 = _enqueue(repo, student_id, aggregate_id="ep_2")
    restarted = OutboxWorker(connection, index=index)
    restarted.run_pending()
    assert repo.get(outbox_2).status == "indexed"
    assert index.all_episode_ids(student_id) == {"ep_1", "ep_2"}


def test_stale_processing_claim_is_reclaimed(
    env: tuple[object, str, OutboxRepository]
) -> None:
    connection, student_id, repo = env
    outbox_id = _enqueue(repo, student_id)
    index = InMemoryMnemisIndex()
    worker = OutboxWorker(connection, index=index)
    worker.run_pending()
    assert repo.get(outbox_id).status == "indexed"

    stale = _enqueue(repo, student_id, aggregate_id="ep_stale")
    repo.connection.execute(
        """
        UPDATE memory_outbox
        SET status = %s, next_attempt_at = %s
        WHERE outbox_id = %s
        """,
        ("processing", (datetime.now(UTC) - timedelta(minutes=5)).isoformat(), stale),
    )
    repo.connection.commit()
    worker.run_pending()
    assert repo.get(stale).status == "indexed"


def test_worker_without_index_leaves_rows_pending(
    env: tuple[object, str, OutboxRepository]
) -> None:
    connection, student_id, repo = env
    outbox_id = _enqueue(repo, student_id)
    worker = OutboxWorker(connection, index=None)
    assert worker.run_pending() == 0
    assert repo.get(outbox_id).status == "pending"


def test_delete_student_operation_removes_indexed_memories(
    env: tuple[object, str, OutboxRepository]
) -> None:
    connection, student_id, repo = env
    _enqueue(repo, student_id)
    index = InMemoryMnemisIndex()
    OutboxWorker(connection, index=index).run_pending()
    assert asyncio.run(index.count_episodes(student_id)) == 1

    repo.enqueue(
        repo.connection,
        student_id=student_id,
        aggregate_type="student",
        aggregate_id=student_id,
        operation="delete_student",
        payload={"student_id": student_id},
        version=1,
    )
    repo.connection.commit()
    worker = OutboxWorker(connection, index=index)
    worker.run_pending()
    assert asyncio.run(index.count_episodes(student_id)) == 0
    assert asyncio.run(index.count_facts(student_id)) == 0