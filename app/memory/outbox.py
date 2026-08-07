"""Transactional memory outbox (MEMORY_CONSISTENCY §3.4, §4, §5).

SQLite is the authoritative store; the outbox is delivery intent for
asynchronous derived indexes (Mnemis). ``enqueue`` must be called inside the
caller's transaction so episode/fact writes and outbox rows commit atomically.
The worker then moves rows pending -> processing -> indexed | retrying ->
dead_letter using the fixed spec schedule.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.infrastructure.database import connect, transaction

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


def _row_to_record(row: sqlite3.Row) -> OutboxRecord:
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
    )


class OutboxRepository:
    def __init__(self, database_path: Path, *, default_student_id: str | None = None) -> None:
        self.database_path = database_path
        self.default_student_id = default_student_id

    def enqueue(
        self,
        connection: sqlite3.Connection,
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
        yields a single row; repeated delivery creates no duplicate index work.
        """
        key = outbox_idempotency_key(student_id, aggregate_type, aggregate_id, version, operation)
        existing = connection.execute(
            "SELECT outbox_id FROM memory_outbox WHERE idempotency_key = ?", (key,)
        ).fetchone()
        if existing is not None:
            return existing["outbox_id"]
        outbox_id = f"out_{uuid.uuid4().hex[:12]}"
        timestamp = now or utc_now_iso()
        connection.execute(
            """
            INSERT INTO memory_outbox (
                outbox_id, student_id, aggregate_type, aggregate_id, operation,
                payload_json, idempotency_key, status, attempt_count,
                next_attempt_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?)
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
        )
        return outbox_id

    def claim_due(
        self,
        *,
        now: str | None = None,
        batch_size: int = 20,
        lease_deadline: str | None = None,
    ) -> list[OutboxRecord]:
        """Claim due rows and mark them processing.

        Claims pending/retrying/deletion_pending rows that are due, plus
        processing rows whose lease expired (crashed worker). A fresh claim
        gets ``lease_deadline`` as its next_attempt_at, so it is not
        double-processed while the lease is alive.
        """
        timestamp = now or utc_now_iso()
        lease = lease_deadline or (
            datetime.fromisoformat(timestamp) + timedelta_seconds(CLAIM_LEASE_SECONDS)
        ).isoformat()
        claimed: list[OutboxRecord] = []
        with connect(self.database_path) as connection:
            with transaction(connection):
                rows = connection.execute(
                    """
                    SELECT * FROM memory_outbox
                    WHERE (status IN ('pending', 'retrying', 'deletion_pending')
                           AND next_attempt_at <= ?)
                       OR (status = 'processing' AND next_attempt_at <= ?)
                    ORDER BY next_attempt_at, created_at
                    LIMIT ?
                    """,
                    (timestamp, timestamp, batch_size),
                ).fetchall()
                for row in rows:
                    connection.execute(
                        """
                        UPDATE memory_outbox
                        SET status = 'processing', next_attempt_at = ?
                        WHERE outbox_id = ?
                        """,
                        (lease, row["outbox_id"]),
                    )
                    claimed.append(_row_to_record(row))
        return claimed

    def complete(self, outbox_id: str, *, now: str | None = None) -> None:
        timestamp = now or utc_now_iso()
        with connect(self.database_path) as connection:
            connection.execute(
                "UPDATE memory_outbox SET status = 'indexed', completed_at = ? WHERE outbox_id = ?",
                (timestamp, outbox_id),
            )

    def mark_deleted(self, outbox_id: str, *, now: str | None = None) -> None:
        timestamp = now or utc_now_iso()
        with connect(self.database_path) as connection:
            connection.execute(
                "UPDATE memory_outbox SET status = 'deleted', completed_at = ? WHERE outbox_id = ?",
                (timestamp, outbox_id),
            )

    def mark_failed(self, outbox_id: str, error: str, *, now: str | None = None) -> str:
        """Record a failed delivery attempt; returns the new status."""
        timestamp = now or utc_now_iso()
        with connect(self.database_path) as connection:
            with transaction(connection):
                row = connection.execute(
                    "SELECT attempt_count FROM memory_outbox WHERE outbox_id = ?", (outbox_id,)
                ).fetchone()
                if row is None:
                    raise KeyError(outbox_id)
                attempts = row["attempt_count"] + 1
                delay = next_retry_delay_seconds(attempts)
                if delay is None:
                    status = "dead_letter"
                    next_attempt = timestamp
                else:
                    status = "retrying"
                    next_attempt = (
                        datetime.fromisoformat(timestamp) + timedelta_seconds(delay)
                    ).isoformat()
                connection.execute(
                    """
                    UPDATE memory_outbox
                    SET status = ?, attempt_count = ?, next_attempt_at = ?,
                        last_error = ?
                    WHERE outbox_id = ?
                    """,
                    (status, attempts, next_attempt, error[:500], outbox_id),
                )
        return status

    def get(self, outbox_id: str) -> OutboxRecord | None:
        with connect(self.database_path) as connection:
            row = connection.execute(
                "SELECT * FROM memory_outbox WHERE outbox_id = ?", (outbox_id,)
            ).fetchone()
        if row is None:
            return None
        return _row_to_record(row)

    def list_by_status(self, status: str) -> list[OutboxRecord]:
        with connect(self.database_path) as connection:
            rows = connection.execute(
                "SELECT * FROM memory_outbox WHERE status = ? ORDER BY created_at", (status,)
            ).fetchall()
        return [_row_to_record(row) for row in rows]

    def consistency_metrics(self, *, now: str | None = None) -> dict:
        """Required metrics from MEMORY_CONSISTENCY §13."""
        timestamp = now or utc_now_iso()
        now_dt = datetime.fromisoformat(timestamp)
        with connect(self.database_path) as connection:
            pending = connection.execute(
                "SELECT COUNT(*) AS c FROM memory_outbox WHERE status = 'pending'"
            ).fetchone()["c"]
            dead = connection.execute(
                "SELECT COUNT(*) AS c FROM memory_outbox WHERE status = 'dead_letter'"
            ).fetchone()["c"]
            oldest_row = connection.execute(
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


def timedelta_seconds(seconds: int) -> timedelta:
    from datetime import timedelta

    return timedelta(seconds=seconds)
