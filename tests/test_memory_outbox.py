"""Transactional memory outbox tests (MEMORY_CONSISTENCY §3.4, §4, §5).

Covers idempotent enqueue inside the caller's transaction, due claiming,
completion, the fixed retry schedule, dead-lettering after five attempts, and
the required consistency metrics.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.infrastructure import migration_runner
from app.infrastructure.database import connect, transaction
from app.infrastructure.learner_store import LearnerStore
from app.memory.outbox import (
    MAX_ATTEMPTS,
    RETRY_DELAY_SECONDS,
    OutboxRepository,
    outbox_idempotency_key,
)


@pytest.fixture()
def repo(tmp_path: Path) -> OutboxRepository:
    db = tmp_path / "outbox.db"
    migration_runner.apply_migrations(db)
    learner = LearnerStore(db)
    student_id, _ = learner.create_student("Ari", 20, 1200)
    return OutboxRepository(db, default_student_id=student_id)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _enqueue(repo: OutboxRepository, *, student_id: str | None = None, version: int = 1) -> str:
    with connect(repo.database_path) as connection:
        with transaction(connection):
            return repo.enqueue(
                connection,
                student_id=student_id or repo.default_student_id,
                aggregate_type="episode",
                aggregate_id="ep_abc",
                operation="upsert_episode",
                payload={"episode_id": "ep_abc"},
                version=version,
            )


def test_enqueue_appends_row_with_stable_idempotency_key(repo: OutboxRepository) -> None:
    outbox_id = _enqueue(repo)
    record = repo.get(outbox_id)
    assert record is not None
    assert record.status == "pending"
    assert record.attempt_count == 0
    assert record.operation == "upsert_episode"
    assert record.aggregate_type == "episode"
    assert record.aggregate_id == "ep_abc"
    assert record.idempotency_key == (
        f"memory-index:{repo.default_student_id}:episode:ep_abc:1:upsert_episode"
    )


def test_re_enqueue_same_operation_is_idempotent(repo: OutboxRepository) -> None:
    first = _enqueue(repo)
    second = _enqueue(repo)
    assert first == second
    with connect(repo.database_path) as connection:
        count = connection.execute("SELECT COUNT(*) FROM memory_outbox").fetchone()[0]
    assert count == 1


def test_version_change_creates_new_row(repo: OutboxRepository) -> None:
    first = _enqueue(repo, version=1)
    second = _enqueue(repo, version=2)
    assert first != second
    assert repo.get(second).idempotency_key.endswith(":2:upsert_episode")


def test_outbox_write_and_episode_write_share_one_transaction(tmp_path: Path) -> None:
    """The enqueue must run inside the caller's transaction: rolling the
    caller back must also roll back the outbox row."""
    db = tmp_path / "tx.db"
    migration_runner.apply_migrations(db)
    learner = LearnerStore(db)
    student_id, _ = learner.create_student("Ari", 20, 1200)
    repo = OutboxRepository(db)
    with pytest.raises(RuntimeError):
        with connect(db) as connection:
            with transaction(connection):
                repo.enqueue(
                    connection,
                    student_id=student_id,
                    aggregate_type="episode",
                    aggregate_id="ep_1",
                    operation="upsert_episode",
                    payload={},
                    version=1,
                )
                raise RuntimeError("caller rolls back")
    with connect(db) as connection:
        count = connection.execute("SELECT COUNT(*) FROM memory_outbox").fetchone()[0]
    assert count == 0


def test_claim_due_returns_and_marks_processing(repo: OutboxRepository) -> None:
    outbox_id = _enqueue(repo)
    claimed = repo.claim_due(now=_now(), batch_size=10)
    assert [c.outbox_id for c in claimed] == [outbox_id]
    assert repo.get(outbox_id).status == "processing"


def test_future_retry_row_is_not_claimed(repo: OutboxRepository) -> None:
    _enqueue(repo)
    with connect(repo.database_path) as connection:
        connection.execute(
            "UPDATE memory_outbox SET status = 'retrying', next_attempt_at = ?",
            ((datetime.now(UTC) + timedelta(hours=1)).isoformat(),),
        )
    assert repo.claim_due(now=_now(), batch_size=10) == []


def test_complete_marks_indexed(repo: OutboxRepository) -> None:
    outbox_id = _enqueue(repo)
    repo.claim_due(now=_now(), batch_size=10)
    repo.complete(outbox_id, now=_now())
    record = repo.get(outbox_id)
    assert record.status == "indexed"
    assert record.completed_at is not None


def test_retry_schedule_matches_spec(repo: OutboxRepository) -> None:
    assert RETRY_DELAY_SECONDS == (0, 5, 30, 300, 1800)
    outbox_id = _enqueue(repo)
    repo.claim_due(now=_now(), batch_size=10)
    for expected_delay in RETRY_DELAY_SECONDS:
        status = repo.mark_failed(outbox_id, "boom", now=_now())
        record = repo.get(outbox_id)
        assert status == "retrying"
        assert record.attempt_count > 0
        actual_delay = (
            datetime.fromisoformat(record.next_attempt_at) - datetime.now(UTC)
        ).total_seconds()
        assert actual_delay <= expected_delay + 2
        assert record.last_error == "boom"
        repo.claim_due(now=_now(), batch_size=10)
    assert record.attempt_count == MAX_ATTEMPTS


def test_dead_letter_after_five_failures(repo: OutboxRepository) -> None:
    outbox_id = _enqueue(repo)
    for _ in range(MAX_ATTEMPTS):
        repo.claim_due(now=_now(), batch_size=10)
        repo.mark_failed(outbox_id, "boom", now=_now())
    repo.claim_due(now=_now(), batch_size=10)
    status = repo.mark_failed(outbox_id, "boom", now=_now())
    assert status == "dead_letter"
    record = repo.get(outbox_id)
    assert record.status == "dead_letter"
    assert record.attempt_count == MAX_ATTEMPTS + 1
    assert repo.claim_due(now=_now(), batch_size=10) == []


def test_consistency_metrics(repo: OutboxRepository) -> None:
    a = _enqueue(repo)
    b = _enqueue(repo)
    repo.claim_due(now=_now(), batch_size=10)
    repo.complete(a, now=_now())
    for _ in range(MAX_ATTEMPTS + 1):
        repo.claim_due(now=_now(), batch_size=10)
        repo.mark_failed(b, "boom", now=_now())
    metrics = repo.consistency_metrics(now=_now())
    assert metrics["outbox_pending_count"] == 0
    assert metrics["outbox_dead_letter_count"] == 1
    assert metrics["outbox_oldest_age_seconds"] is None


def test_oldest_pending_age(repo: OutboxRepository) -> None:
    created = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
    with connect(repo.database_path) as connection:
        with transaction(connection):
            repo.enqueue(
                connection,
                student_id=repo.default_student_id,
                aggregate_type="episode",
                aggregate_id="ep_old",
                operation="upsert_episode",
                payload={},
                version=1,
                now=created,
            )
    age = repo.consistency_metrics(now=_now())["outbox_oldest_age_seconds"]
    assert age is not None and age >= 595
