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
from tests.pg_test_helpers import cleanup_tenant, unique_tenant_id


@pytest.fixture()
def repo():
    admin = pg.connect_admin()
    migrate_database(admin)
    admin.close()
    tenant_id = unique_tenant_id("task3_pg_outbox")
    conn = pg.connect()
    conn.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant_id,))
    conn.commit()
    learner = LearnerStore(conn)
    student_id, _ = learner.create_student("Ari", 20, 1200)
    yield OutboxRepository(conn, default_student_id=student_id)
    conn.close()
    cleanup = pg.connect_admin()
    try:
        cleanup_tenant(cleanup, tenant_id)
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
        cleanup.rollback()
        cleanup_tenant(cleanup, f"tenant_{channel}_a")
        cleanup_tenant(cleanup, f"tenant_{channel}_b")
        cleanup.close()


def test_outbox_write_rolls_back_with_caller_transaction() -> None:
    """The enqueue must run inside the caller's transaction: rolling the
    caller back must also roll back the outbox row."""
    admin = pg.connect_admin()
    migrate_database(admin)
    admin.close()
    tenant_id = unique_tenant_id("task3_pg_outbox_rollback")
    conn = pg.connect()
    conn.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant_id,))
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
        cleanup_tenant(cleanup, tenant_id)
    finally:
        cleanup.close()


def test_claim_due_returns_and_marks_processing(repo: OutboxRepository) -> None:
    outbox_id = _enqueue(repo)
    claimed = repo.claim_due(now=_now(), batch_size=10)
    assert [c.outbox_id for c in claimed] == [outbox_id]
    assert repo.get(outbox_id).status == "processing"


def test_array_filters_bind_sorted_python_lists(repo: OutboxRepository) -> None:
    first = _enqueue(repo, version=1)
    second = _enqueue(repo, version=2)

    class RecordingConnection:
        def __init__(self, connection) -> None:  # noqa: ANN001
            self.connection = connection
            self.array_params: list[list[str]] = []

        def execute(self, query, params=None, **kwargs):  # noqa: ANN001
            if "ANY(%s)" in str(query):
                self.array_params.extend(
                    value for value in (params or []) if isinstance(value, list)
                )
            return self.connection.execute(query, params, **kwargs)

        def __getattr__(self, name: str):
            return getattr(self.connection, name)

    recording = RecordingConnection(repo.connection)
    filtered = OutboxRepository(recording, default_student_id=repo.default_student_id)
    ids = [second, first]
    assert filtered.due_student_id(
        now=_now(), outbox_ids=ids, exclude_student_ids=["z", "a"]
    ) == repo.default_student_id
    filtered.claim_due(now=_now(), outbox_ids=ids, batch_size=10)

    assert recording.array_params[0] == sorted(ids)
    assert recording.array_params[1] == ["a", "z"]
    assert recording.array_params[2] == sorted(ids)


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


def test_terminal_delivery_clears_stale_error(repo: OutboxRepository) -> None:
    indexed_id = _enqueue(repo, version=3)
    indexed_token = _claim_token(repo, indexed_id)
    assert repo.mark_failed(
        indexed_id, indexed_token, "stale failure", now=_now()
    ) == "retrying"
    retry_token = _claim_token(repo, indexed_id)
    assert repo.complete(indexed_id, retry_token, now=_now()) is True
    assert repo.get(indexed_id).last_error is None

    deleted_id = repo.enqueue(
        repo.connection,
        student_id=repo.default_student_id,
        aggregate_type="student",
        aggregate_id=repo.default_student_id,
        operation="delete_student",
        payload={"student_id": repo.default_student_id},
        version=1,
    )
    repo.connection.commit()
    deleted_token = _claim_token(repo, deleted_id)
    assert repo.mark_failed(
        deleted_id, deleted_token, "stale delete failure", now=_now()
    ) == "retrying"
    retry_token = _claim_token(repo, deleted_id)
    assert repo.mark_deleted(deleted_id, retry_token, now=_now()) is True
    record = repo.get(deleted_id)
    assert record.status == "deleted"
    assert record.last_error is None


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


def _claim_after_lease_expiry(
    tenant_id: str, student_id: str, outbox_id: str
) -> tuple[OutboxRepository, OutboxRepository, object, object]:
    """Claim on A, force the lease due, then reclaim on B; return both repos
    and the two claim records so tests can prove stale transitions lose."""
    first = pg.connect()
    first.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant_id,))
    first.commit()
    repo_a = OutboxRepository(first)
    (row_a,) = repo_a.claim_due(now=_now(), batch_size=10)
    first.execute(
        "UPDATE memory_outbox SET next_attempt_at = %s WHERE outbox_id = %s",
        ((datetime.now(UTC) - timedelta(minutes=5)).isoformat(), outbox_id),
    )
    first.commit()

    second = pg.connect()
    second.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant_id,))
    second.commit()
    repo_b = OutboxRepository(second)
    (row_b,) = repo_b.claim_due(now=_now(), batch_size=10)
    assert row_a.outbox_id == row_b.outbox_id == outbox_id
    return repo_a, repo_b, row_a, row_b


def test_stale_worker_cannot_complete_reclaimed_claim() -> None:
    """A worker whose lease expired must not complete the row after another
    worker reclaimed it: transitions require the current claim token."""
    admin = pg.connect_admin()
    migrate_database(admin)
    admin.close()
    tenant_id = unique_tenant_id("task3_stale_complete")
    conn = pg.connect()
    conn.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant_id,))
    conn.commit()
    learner = LearnerStore(conn)
    student_id, _ = learner.create_student("Ari", 20, 1200)
    repo = OutboxRepository(conn)
    outbox_id = _enqueue(repo, student_id=student_id)
    try:
        repo_a, repo_b, row_a, row_b = _claim_after_lease_expiry(
            tenant_id, student_id, outbox_id
        )
        assert row_a.claim_token is not None
        assert row_a.claim_token != row_b.claim_token

        assert repo_a.complete(outbox_id, row_a.claim_token, now=_now()) is False
        assert repo.get(outbox_id).status == "processing"
        assert repo_b.complete(outbox_id, row_b.claim_token, now=_now()) is True
        assert repo.get(outbox_id).status == "indexed"
        assert repo.get(outbox_id).claim_token is None
    finally:
        conn.close()
        cleanup = pg.connect_admin()
        try:
            cleanup_tenant(cleanup, tenant_id)
        finally:
            cleanup.close()


def test_stale_worker_cannot_fail_reclaimed_claim() -> None:
    """A stale failure transition must not change state or bump attempts."""
    admin = pg.connect_admin()
    migrate_database(admin)
    admin.close()
    tenant_id = unique_tenant_id("task3_stale_failed")
    conn = pg.connect()
    conn.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant_id,))
    conn.commit()
    learner = LearnerStore(conn)
    student_id, _ = learner.create_student("Ari", 20, 1200)
    repo = OutboxRepository(conn)
    outbox_id = _enqueue(repo, student_id=student_id)
    try:
        repo_a, repo_b, row_a, row_b = _claim_after_lease_expiry(
            tenant_id, student_id, outbox_id
        )
        assert repo_a.mark_failed(
            outbox_id, row_a.claim_token, "stale boom", now=_now()
        ) is None
        record = repo.get(outbox_id)
        assert record.status == "processing"
        assert record.attempt_count == 0
        assert record.last_error is None
        assert repo_b.mark_failed(
            outbox_id, row_b.claim_token, "real boom", now=_now()
        ) == "retrying"
        record = repo.get(outbox_id)
        assert record.status == "retrying"
        assert record.attempt_count == 1
        assert record.last_error == "real boom"
        assert record.claim_token is None
    finally:
        conn.close()
        cleanup = pg.connect_admin()
        try:
            cleanup_tenant(cleanup, tenant_id)
        finally:
            cleanup.close()


def test_stale_worker_cannot_mark_deleted_reclaimed_claim() -> None:
    """A delete transition is claim-owned too and must end in deleted."""
    admin = pg.connect_admin()
    migrate_database(admin)
    admin.close()
    tenant_id = unique_tenant_id("task3_stale_deleted")
    conn = pg.connect()
    conn.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant_id,))
    conn.commit()
    learner = LearnerStore(conn)
    student_id, _ = learner.create_student("Ari", 20, 1200)
    repo = OutboxRepository(conn)
    outbox_id = repo.enqueue(
        repo.connection,
        student_id=student_id,
        aggregate_type="student",
        aggregate_id=student_id,
        operation="delete_student",
        payload={"student_id": student_id},
        version=1,
    )
    repo.connection.commit()
    try:
        repo_a, repo_b, row_a, row_b = _claim_after_lease_expiry(
            tenant_id, student_id, outbox_id
        )
        assert repo_a.mark_deleted(outbox_id, row_a.claim_token, now=_now()) is False
        assert repo.get(outbox_id).status == "processing"
        assert repo_b.mark_deleted(outbox_id, row_b.claim_token, now=_now()) is True
        record = repo.get(outbox_id)
        assert record.status == "deleted"
        assert record.claim_token is None
    finally:
        conn.close()
        cleanup = pg.connect_admin()
        try:
            cleanup_tenant(cleanup, tenant_id)
        finally:
            cleanup.close()
