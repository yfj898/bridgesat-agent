"""Transactional memory outbox (MEMORY_CONSISTENCY §3.4, §4, §5).

PostgreSQL is the authoritative store; the outbox is delivery intent for
asynchronous derived indexes (Mnemis). ``enqueue`` must be called inside the
caller's transaction so episode/fact writes and outbox rows commit atomically.
The worker then moves rows pending -> processing -> indexed | retrying ->
dead_letter using the fixed spec schedule.
"""

from __future__ import annotations

import json
import asyncio
import uuid
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import AsyncIterator, Iterator, Sequence

import psycopg

OUTBOX_STATUSES = (
    "pending",
    "processing",
    "indexed",
    "retrying",
    "dead_letter",
    "deletion_pending",
    "deleted",
)

RETRY_DELAY_SECONDS: tuple[int, ...] = (0, 5, 30, 300, 1800)
MAX_ATTEMPTS = len(RETRY_DELAY_SECONDS)
CLAIM_LEASE_SECONDS = 60
STUDENT_LOCK_NAMESPACE = "bridgesat:memory:student:"


def student_lock_key(student_id: str) -> str:
    return f"{STUDENT_LOCK_NAMESPACE}{student_id}"


def ensure_active_student(
    connection: psycopg.Connection, student_id: str
) -> None:
    """Lock and validate the authoritative student before memory writes."""
    student = connection.execute(
        """
        SELECT status
        FROM students
        WHERE id = %s
          AND tenant_id = current_setting('app.tenant_id', true)
        FOR UPDATE
        """,
        (student_id,),
    ).fetchone()
    if student is None:
        raise ValueError(f"Student {student_id} does not belong to the current tenant")
    if student["status"] != "active":
        raise ValueError(
            f"Student {student_id} is not active (status={student['status']})"
        )
    deletion = connection.execute(
        """
        SELECT state
        FROM student_deletions
        WHERE student_id = %s
          AND tenant_id = current_setting('app.tenant_id', true)
        FOR UPDATE
        """,
        (student_id,),
    ).fetchone()
    if deletion is not None:
        raise ValueError(
            f"Student {student_id} has a deletion state ({deletion['state']})"
        )


def _close_connection(connection: psycopg.Connection) -> None:
    try:
        connection.close()
    except BaseException:
        pass


def _release_student_lock(connection: psycopg.Connection, key: str) -> None:
    row = connection.execute(
        "SELECT pg_advisory_unlock(hashtextextended(%s, 0)) AS unlocked",
        (key,),
    ).fetchone()
    if row is None or not row["unlocked"]:
        raise RuntimeError("student advisory lock was not held during release")


@contextmanager
def student_advisory_lock(
    connection: psycopg.Connection, student_id: str
) -> Iterator[None]:
    """Hold the session-level lock shared by rebuilds and outbox workers."""
    key = student_lock_key(student_id)
    acquired = False
    primary_error: BaseException | None = None
    try:
        connection.execute(
            "SELECT pg_advisory_lock(hashtextextended(%s, 0))", (key,)
        )
        acquired = True
        yield
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        if acquired:
            try:
                _release_student_lock(connection, key)
            except BaseException:
                _close_connection(connection)
                if primary_error is None:
                    raise
        elif primary_error is not None:
            _close_connection(connection)


@asynccontextmanager
async def student_advisory_lock_async(
    connection: psycopg.Connection,
    student_id: str,
    *,
    poll_interval: float = 0.05,
) -> AsyncIterator[None]:
    """Poll for the shared student lock without blocking the event loop."""
    key = student_lock_key(student_id)
    acquired = False
    cancelled = False
    primary_error: BaseException | None = None
    try:
        while not acquired:
            row = connection.execute(
                "SELECT pg_try_advisory_lock(hashtextextended(%s, 0)) AS acquired",
                (key,),
            ).fetchone()
            acquired = row is not None and bool(row["acquired"])
            if not acquired:
                await asyncio.sleep(poll_interval)
        try:
            yield
        except asyncio.CancelledError as exc:
            cancelled = True
            primary_error = exc
            raise
        except BaseException as exc:
            primary_error = exc
            raise
    except asyncio.CancelledError as exc:
        cancelled = True
        primary_error = exc
        raise
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        release_error: BaseException | None = None
        if acquired:
            try:
                _release_student_lock(connection, key)
            except BaseException as exc:
                release_error = exc
        if cancelled or release_error is not None or (not acquired and primary_error is not None):
            _close_connection(connection)
        if release_error is not None and primary_error is None:
            raise release_error


@dataclass
class OutboxRecord:
    outbox_id: str
    student_id: str
    aggregate_type: str
    aggregate_id: str
    operation: str
    payload: dict
    idempotency_key: str
    status: str
    attempt_count: int
    next_attempt_at: str
    last_error: str | None
    created_at: str
    completed_at: str | None
    claim_token: str | None = None


def outbox_idempotency_key(
    student_id: str, aggregate_type: str, aggregate_id: str, version: int, operation: str
) -> str:
    return f"memory-index:{student_id}:{aggregate_type}:{aggregate_id}:{version}:{operation}"


def next_retry_delay_seconds(attempt_count: int) -> int | None:
    """Fixed spec schedule; attempt_count is the count after incrementing.

    Returns None when the record must be dead-lettered instead of retried.
    """
    if attempt_count <= 0:
        return 0
    if attempt_count > MAX_ATTEMPTS:
        return None
    return RETRY_DELAY_SECONDS[attempt_count - 1]


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _row_to_record(row: dict) -> OutboxRecord:
    return OutboxRecord(
        outbox_id=row["outbox_id"],
        student_id=row["student_id"],
        aggregate_type=row["aggregate_type"],
        aggregate_id=row["aggregate_id"],
        operation=row["operation"],
        payload=json.loads(row["payload_json"] or "{}"),
        idempotency_key=row["idempotency_key"],
        status=row["status"],
        attempt_count=row["attempt_count"],
        next_attempt_at=row["next_attempt_at"],
        last_error=row["last_error"],
        created_at=row["created_at"],
        completed_at=row["completed_at"],
        claim_token=row.get("claim_token"),
    )


class OutboxRepository:
    def __init__(self, connection: psycopg.Connection, *, default_student_id: str | None = None) -> None:
        self.connection = connection
        self.default_student_id = default_student_id

    def enqueue(
        self,
        connection: psycopg.Connection,
        *,
        student_id: str,
        aggregate_type: str,
        aggregate_id: str,
        operation: str,
        payload: dict,
        version: int,
        now: str | None = None,
    ) -> str:
        """Insert a pending outbox row inside the caller's transaction.

        Idempotent: the same (student, aggregate, version, operation) pair
        yields a single row; repeated delivery creates no duplicate index
        work. The unique (tenant_id, idempotency_key) index absorbs races.
        """
        key = outbox_idempotency_key(student_id, aggregate_type, aggregate_id, version, operation)
        outbox_id = f"out_{uuid.uuid4().hex[:12]}"
        timestamp = now or utc_now_iso()
        inserted = connection.execute(
            """
            INSERT INTO memory_outbox (
                outbox_id, tenant_id, student_id, aggregate_type, aggregate_id,
                operation, payload_json, idempotency_key, status, attempt_count,
                next_attempt_at, created_at
            ) VALUES (
                %s, current_setting('app.tenant_id'), %s, %s, %s, %s, %s, %s,
                'pending', 0, %s, %s
            )
            ON CONFLICT (tenant_id, idempotency_key) DO NOTHING
            RETURNING outbox_id
            """,
            (
                outbox_id,
                student_id,
                aggregate_type,
                aggregate_id,
                operation,
                json.dumps(payload, sort_keys=True),
                key,
                timestamp,
                timestamp,
            ),
        ).fetchone()
        if inserted is not None:
            return inserted["outbox_id"]
        existing = connection.execute(
            "SELECT outbox_id FROM memory_outbox WHERE idempotency_key = %s",
            (key,),
        ).fetchone()
        if existing is None:
            raise RuntimeError("outbox enqueue conflict without existing row")
        return existing["outbox_id"]

    def claim_due(
        self,
        *,
        now: str | None = None,
        batch_size: int = 20,
        lease_deadline: str | None = None,
        student_id: str | None = None,
        outbox_ids: Sequence[str] | None = None,
    ) -> list[OutboxRecord]:
        """Claim due rows and mark them processing.

        Claims pending/retrying/deletion_pending rows that are due, plus
        processing rows whose lease expired (crashed worker). A fresh claim
        gets ``lease_deadline`` as its next_attempt_at, so it is not
        double-processed while the lease is alive. SKIP LOCKED keeps
        concurrent workers from double-claiming.
        """
        timestamp = now or utc_now_iso()
        if outbox_ids is not None and not outbox_ids:
            return []
        lease = lease_deadline or (
            datetime.fromisoformat(timestamp) + timedelta(seconds=CLAIM_LEASE_SECONDS)
        ).isoformat()
        conditions = [
            "tenant_id = current_setting('app.tenant_id')",
            "((status IN ('pending', 'retrying', 'deletion_pending') "
            "AND next_attempt_at <= %s) "
            "OR (status = 'processing' AND next_attempt_at <= %s))",
        ]
        params: list[object] = [timestamp, timestamp]
        if student_id is not None:
            conditions.append("student_id = %s")
            params.append(student_id)
        if outbox_ids is not None:
            conditions.append("outbox_id = ANY(%s)")
            params.append(sorted(outbox_ids))
        params.append(batch_size)
        try:
            rows = self.connection.execute(
                f"""
                SELECT * FROM memory_outbox
                WHERE {' AND '.join(conditions)}
                ORDER BY next_attempt_at, created_at
                LIMIT %s
                FOR UPDATE SKIP LOCKED
                """,
                params,
            ).fetchall()
            claimed: list[OutboxRecord] = []
            for row in rows:
                claim_token = uuid.uuid4().hex
                self.connection.execute(
                    """
                    UPDATE memory_outbox
                    SET status = 'processing', next_attempt_at = %s,
                        claim_token = %s
                    WHERE outbox_id = %s
                      AND tenant_id = current_setting('app.tenant_id')
                    """,
                    (lease, claim_token, row["outbox_id"]),
                )
                record = _row_to_record(row)
                record.claim_token = claim_token
                claimed.append(record)
            self.connection.commit()
            return claimed
        except BaseException:
            self.connection.rollback()
            raise

    def due_student_id(
        self,
        *,
        now: str | None = None,
        student_id: str | None = None,
        outbox_ids: Sequence[str] | None = None,
        exclude_student_ids: Sequence[str] | None = None,
    ) -> str | None:
        """Return one due student, optionally restricted to outbox IDs."""
        if outbox_ids is not None and not outbox_ids:
            return None
        if exclude_student_ids is not None and not exclude_student_ids:
            exclude_student_ids = None
        timestamp = now or utc_now_iso()
        conditions = [
            "tenant_id = current_setting('app.tenant_id')",
            "((status IN ('pending', 'retrying', 'deletion_pending') "
            "AND next_attempt_at <= %s) "
            "OR (status = 'processing' AND next_attempt_at <= %s))",
        ]
        params: list[object] = [timestamp, timestamp]
        if student_id is not None:
            conditions.append("student_id = %s")
            params.append(student_id)
        if outbox_ids is not None:
            conditions.append("outbox_id = ANY(%s)")
            params.append(sorted(outbox_ids))
        if exclude_student_ids is not None:
            conditions.append("NOT (student_id = ANY(%s))")
            params.append(sorted(exclude_student_ids))
        row = self.connection.execute(
            f"""
            SELECT student_id
            FROM memory_outbox
            WHERE {' AND '.join(conditions)}
            ORDER BY next_attempt_at, created_at, student_id
            LIMIT 1
            """,
            params,
        ).fetchone()
        return row["student_id"] if row is not None else None

    def complete(
        self, outbox_id: str, claim_token: str, *, now: str | None = None
    ) -> bool:
        """Mark an owned processing claim indexed; False when the claim is
        stale (row re-claimed by another worker or no longer processing)."""
        timestamp = now or utc_now_iso()
        try:
            updated = self.connection.execute(
                "UPDATE memory_outbox SET status = %s, completed_at = %s, "
                "last_error = NULL, claim_token = NULL "
                "WHERE outbox_id = %s "
                "AND tenant_id = current_setting('app.tenant_id') "
                "AND status = 'processing' AND claim_token = %s",
                ("indexed", timestamp, outbox_id, claim_token),
            ).rowcount
            self.connection.commit()
            return updated == 1
        except BaseException:
            self.connection.rollback()
            raise

    def mark_deleted(
        self, outbox_id: str, claim_token: str, *, now: str | None = None
    ) -> bool:
        """Mark an owned delete claim terminal (deleted); False when stale."""
        timestamp = now or utc_now_iso()
        try:
            updated = self.connection.execute(
                "UPDATE memory_outbox SET status = %s, completed_at = %s, "
                "last_error = NULL, claim_token = NULL "
                "WHERE outbox_id = %s "
                "AND tenant_id = current_setting('app.tenant_id') "
                "AND status = 'processing' AND claim_token = %s",
                ("deleted", timestamp, outbox_id, claim_token),
            ).rowcount
            self.connection.commit()
            return updated == 1
        except BaseException:
            self.connection.rollback()
            raise

    def mark_failed(
        self,
        outbox_id: str,
        claim_token: str,
        error: str,
        *,
        now: str | None = None,
    ) -> str | None:
        """Record a failed delivery attempt; returns the new status, or None
        when the claim is stale and no state changed."""
        timestamp = now or utc_now_iso()
        try:
            row = self.connection.execute(
                "SELECT attempt_count FROM memory_outbox WHERE outbox_id = %s "
                "AND tenant_id = current_setting('app.tenant_id') "
                "AND status = 'processing' AND claim_token = %s",
                (outbox_id, claim_token),
            ).fetchone()
            if row is None:
                self.connection.rollback()
                return None
            attempts = row["attempt_count"] + 1
            delay = next_retry_delay_seconds(attempts)
            if delay is None:
                status = "dead_letter"
                next_attempt = timestamp
            else:
                status = "retrying"
                next_attempt = (
                    datetime.fromisoformat(timestamp) + timedelta(seconds=delay)
                ).isoformat()
            updated = self.connection.execute(
                """
                UPDATE memory_outbox
                SET status = %s, attempt_count = %s, next_attempt_at = %s,
                    last_error = %s, claim_token = NULL
                WHERE outbox_id = %s
                  AND tenant_id = current_setting('app.tenant_id')
                  AND status = 'processing' AND claim_token = %s
                """,
                (status, attempts, next_attempt, error[:500], outbox_id, claim_token),
            ).rowcount
            self.connection.commit()
            if updated != 1:
                return None
            return status
        except BaseException:
            self.connection.rollback()
            raise

    def get(self, outbox_id: str) -> OutboxRecord | None:
        row = self.connection.execute(
            "SELECT * FROM memory_outbox WHERE outbox_id = %s", (outbox_id,)
        ).fetchone()
        if row is None:
            return None
        return _row_to_record(row)

    def list_by_status(self, status: str) -> list[OutboxRecord]:
        rows = self.connection.execute(
            "SELECT * FROM memory_outbox WHERE status = %s ORDER BY created_at",
            (status,),
        ).fetchall()
        return [_row_to_record(row) for row in rows]

    def consistency_metrics(self, *, now: str | None = None) -> dict:
        """Required metrics from MEMORY_CONSISTENCY §13."""
        timestamp = now or utc_now_iso()
        now_dt = datetime.fromisoformat(timestamp)
        pending = self.connection.execute(
            "SELECT COUNT(*) AS c FROM memory_outbox WHERE status = %s",
            ("pending",),
        ).fetchone()["c"]
        dead = self.connection.execute(
            "SELECT COUNT(*) AS c FROM memory_outbox WHERE status = %s",
            ("dead_letter",),
        ).fetchone()["c"]
        oldest_row = self.connection.execute(
            """
            SELECT created_at FROM memory_outbox
            WHERE status = 'pending'
            ORDER BY created_at ASC LIMIT 1
            """
        ).fetchone()
        oldest_age = None
        if oldest_row is not None:
            oldest_age = max(
                0.0, (now_dt - datetime.fromisoformat(oldest_row["created_at"])).total_seconds()
            )
        return {
            "outbox_pending_count": pending,
            "outbox_dead_letter_count": dead,
            "outbox_oldest_age_seconds": oldest_age,
        }
