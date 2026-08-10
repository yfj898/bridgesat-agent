"""Transactional memory outbox tests on PostgreSQL (MEMORY_CONSISTENCY §3.4,
§4, §5).

Covers idempotent enqueue inside the caller's transaction, due claiming,
completion, the fixed retry schedule, dead-lettering after five attempts, and
the required consistency metrics.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.infrastructure import pg
from app.infrastructure.learner_store import LearnerStore
from app.infrastructure.migration_runner import migrate_database
from app.memory.outbox import (
    MAX_ATTEMPTS,
    RETRY_DELAY_SECONDS,
    OutboxRepository,
    outbox_idempotency_key,
)


@pytest.fixture()
def repo() -> OutboxRepository:
    admin = pg.connect_admin()
    migrate_database(admin)
    admin.close()
    conn = pg.connect()
    conn.execute("SELECT set_config('app.tenant_id', %s, false)", ("tenant_test",))
    conn.commit()
    learner = LearnerStore(conn)
    student_id, _ = learner.create_student("Ari", 20, 1200)
    yield OutboxRepository(conn, default_student_id=student_id)
    conn.close()
    cleanup = pg.connect_admin()
    try:
        cleanup.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
        cleanup.commit()
    finally:
        cleanup.close()


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _enqueue(repo: OutboxRepository, *, student_id: str | None = None, version: int = 1) -> str:
    outbox_id = repo.enqueue(
        repo.connection,
        student_id=student_id or repo.default_student_id,
        aggregate_type="episode",
        aggregate_id="ep_abc",
        operation="upsert_episode",
        payload={"episode_id": "ep_abc"},
        version=version,
    )
    repo.connection.commit()
    return outbox_id


def _claim_token(
    repo: OutboxRepository, outbox_id: str, *, now: str | None = None
) -> str:
    claimed = repo.claim_due(now=now or _now(), batch_size=10, outbox_ids=[outbox_id])
    assert len(claimed) == 1
    return claimed[0].claim_token


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
    count = repo.connection.execute("SELECT COUNT(*) AS c FROM memory_outbox").fetchone()["c"]
    assert count == 1


def test_version_change_creates_new_row(repo: OutboxRepository) -> None:
    first = _enqueue(repo, version=1)
    second = _enqueue(repo, version=2)
    assert first != second
    assert repo.get(second).idempotency_key.endswith(":2:upsert_episode")


def test_outbox_write_and_episode_write_share_one_transaction() -> None:
    """The enqueue must run inside the caller's transaction: rolling the
    caller back must also roll back the outbox row."""
    admin = pg.connect_admin()
    migrate_database(admin)
    admin.close()
    conn = pg.connect()
    conn.execute("SELECT set_config('app.tenant_id', %s, false)", ("tenant_test",))
    conn.commit()
    repo = OutboxRepository(conn)
    try:
        with pytest.raises(RuntimeError):
            with pg.transaction(conn):
                repo.enqueue(
                    conn,
                    student_id="stu_tx",
                    aggregate_type="episode",
                    aggregate_id="ep_1",
                    operation="upsert_episode",
                    payload={},
                    version=1,
                )
                raise RuntimeError("caller rolls back")
    finally:
        pass
    count = conn.execute("SELECT COUNT(*) AS c FROM memory_outbox").fetchone()["c"]
    assert count == 0
    conn.close()
    cleanup = pg.connect_admin()
    try:
        cleanup.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
        cleanup.commit()
    finally:
        cleanup.close()


def test_claim_due_returns_and_marks_processing(repo: OutboxRepository) -> None:
    outbox_id = _enqueue(repo)
    claimed = repo.claim_due(now=_now(), batch_size=10)
    assert [c.outbox_id for c in claimed] == [outbox_id]
    assert repo.get(outbox_id).status == "processing"


def test_future_retry_row_is_not_claimed(repo: OutboxRepository) -> None:
    _enqueue(repo)
    repo.connection.execute(
        "UPDATE memory_outbox SET status = %s, next_attempt_at = %s WHERE status = %s",
        ("retrying", (datetime.now(UTC) + timedelta(hours=1)).isoformat(), "pending"),
    )
    repo.connection.commit()
    assert repo.claim_due(now=_now(), batch_size=10) == []


def test_complete_marks_indexed(repo: OutboxRepository) -> None:
    outbox_id = _enqueue(repo)
    claim_token = _claim_token(repo, outbox_id)
    assert repo.complete(outbox_id, claim_token, now=_now()) is True
    record = repo.get(outbox_id)
    assert record.status == "indexed"
    assert record.completed_at is not None


def test_retry_schedule_matches_spec(repo: OutboxRepository) -> None:
    assert RETRY_DELAY_SECONDS == (0, 5, 30, 300, 1800)
    outbox_id = _enqueue(repo)
    now = _now()
    record = None
    for expected_delay in RETRY_DELAY_SECONDS:
        claim_token = _claim_token(repo, outbox_id, now=now)
        status = repo.mark_failed(outbox_id, claim_token, "boom", now=now)
        record = repo.get(outbox_id)
        assert status == "retrying"
        assert record.attempt_count > 0
        actual_delay = (
            datetime.fromisoformat(record.next_attempt_at) - datetime.fromisoformat(now)
        ).total_seconds()
        assert actual_delay <= expected_delay + 2
        assert record.last_error == "boom"
        now = (
            datetime.fromisoformat(record.next_attempt_at) + timedelta(seconds=1)
        ).isoformat()
    assert record.attempt_count == MAX_ATTEMPTS


def test_dead_letter_after_five_failures(repo: OutboxRepository) -> None:
    outbox_id = _enqueue(repo)
    now = _now()
    for _ in range(MAX_ATTEMPTS):
        claim_token = _claim_token(repo, outbox_id, now=now)
        repo.mark_failed(outbox_id, claim_token, "boom", now=now)
        record = repo.get(outbox_id)
        now = (
            datetime.fromisoformat(record.next_attempt_at) + timedelta(seconds=1)
        ).isoformat()
    claim_token = _claim_token(repo, outbox_id, now=now)
    status = repo.mark_failed(outbox_id, claim_token, "boom", now=now)
    assert status == "dead_letter"
    record = repo.get(outbox_id)
    assert record.status == "dead_letter"
    assert record.attempt_count == MAX_ATTEMPTS + 1
    assert repo.claim_due(now=_now(), batch_size=10) == []


def test_consistency_metrics(repo: OutboxRepository) -> None:
    a = _enqueue(repo, version=1)
    b = _enqueue(repo, version=2)
    a_token = _claim_token(repo, a)
    b_token = _claim_token(repo, b)
    assert repo.complete(a, a_token, now=_now()) is True
    now = _now()
    claim_token = b_token
    for _ in range(MAX_ATTEMPTS + 1):
        repo.mark_failed(b, claim_token, "boom", now=now)
        record = repo.get(b)
        now = (
            datetime.fromisoformat(record.next_attempt_at) + timedelta(seconds=1)
        ).isoformat()
        if record.status != "dead_letter":
            claim_token = _claim_token(repo, b, now=now)
    metrics = repo.consistency_metrics(now=_now())
    assert metrics["outbox_pending_count"] == 0
    assert metrics["outbox_dead_letter_count"] == 1
    assert metrics["outbox_oldest_age_seconds"] is None


def test_oldest_pending_age(repo: OutboxRepository) -> None:
    created = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
    repo.enqueue(
        repo.connection,
        student_id=repo.default_student_id,
        aggregate_type="episode",
        aggregate_id="ep_old",
        operation="upsert_episode",
        payload={},
        version=1,
        now=created,
    )
    repo.connection.commit()
    age = repo.consistency_metrics(now=_now())["outbox_oldest_age_seconds"]
    assert age is not None and age >= 595


def test_outbox_idempotency_key_helper() -> None:
    assert outbox_idempotency_key("s", "episode", "e1", 3, "upsert_episode") == (
        "memory-index:s:episode:e1:3:upsert_episode"
    )