"""OutboxRepository on PostgreSQL.

Ports the transactional outbox contract (MEMORY_CONSISTENCY §3.4, §4, §5):
idempotent enqueue inside the caller's transaction, due claiming with lease,
completion, the fixed retry schedule, dead-lettering after five attempts,
tenant isolation, and the consistency metrics.
"""

from __future__ import annotations

import uuid
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

TENANT = "tenant_test"


@pytest.fixture()
def repo():
    admin = pg.connect_admin()
    migrate_database(admin)
    admin.close()
    conn = pg.connect()
    conn.execute("SELECT set_config('app.tenant_id', %s, false)", (TENANT,))
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


def test_cross_tenant_same_key_generates_separate_rows() -> None:
    """Idempotency is per tenant: identical keys in different tenants must
    not collide under RLS or the unique (tenant_id, idempotency_key) index."""
    admin = pg.connect_admin()
    migrate_database(admin)
    admin.close()

    channel = uuid.uuid4().hex[:8]
    students: dict[str, str] = {}
    for tenant in (f"tenant_{channel}_a", f"tenant_{channel}_b"):
        conn = pg.connect()
        conn.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant,))
        conn.commit()
        learner = LearnerStore(conn)
        student_id, _ = learner.create_student("Ari", 20, 1200)
        repo = OutboxRepository(conn, default_student_id=student_id)
        outbox_id = _enqueue(repo)
        students[tenant] = (outbox_id, student_id)
        conn.close()

    cleanup = pg.connect_admin()
    try:
        rows = cleanup.execute(
            "SELECT tenant_id FROM memory_outbox WHERE student_id = %s",
            (students[f"tenant_{channel}_a"][1],),
        ).fetchall()
        cleanup.rollback()
        assert {r["tenant_id"] for r in rows} == {f"tenant_{channel}_a"}
    finally:
        cleanup.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
        cleanup.commit()
        cleanup.close()


def test_outbox_write_rolls_back_with_caller_transaction() -> None:
    """The enqueue must run inside the caller's transaction: rolling the
    caller back must also roll back the outbox row."""
    admin = pg.connect_admin()
    migrate_database(admin)
    admin.close()
    conn = pg.connect()
    conn.execute("SELECT set_config('app.tenant_id', %s, false)", (TENANT,))
    conn.commit()
    repo = OutboxRepository(conn)
    try:
        with pg.transaction(conn):
            repo.enqueue(
                conn,
                student_id="stu_rb",
                aggregate_type="episode",
                aggregate_id="ep_1",
                operation="upsert_episode",
                payload={},
                version=1,
            )
            raise RuntimeError("caller rolls back")
    except RuntimeError:
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
    repo.claim_due(now=_now(), batch_size=10)
    repo.complete(outbox_id, now=_now())
    record = repo.get(outbox_id)
    assert record.status == "indexed"
    assert record.completed_at is not None


def test_retry_schedule_matches_spec(repo: OutboxRepository) -> None:
    assert RETRY_DELAY_SECONDS == (0, 5, 30, 300, 1800)
    outbox_id = _enqueue(repo)
    repo.claim_due(now=_now(), batch_size=10)
    record = None
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