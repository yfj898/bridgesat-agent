"""In-process memory outbox worker (MEMORY_CONSISTENCY §4, §13).

Claims due rows and delivers them to the configured index (Mnemis adapter or
in-memory stub), moving each row pending -> processing -> indexed, or
retrying -> dead_letter on the fixed spec schedule. Delivering is idempotent
via the stable idempotency key. A crashed claim is reclaimed after its lease
expires, so a restart resumes delivery. In local mode (index=None) rows stay
pending and the learning loop is untouched.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any, Sequence

import psycopg

from .outbox import (
    CLAIM_LEASE_SECONDS,
    OutboxRepository,
    student_advisory_lock,
    student_advisory_lock_async,
    utc_now_iso,
)

DISPATCH_BY_OPERATION = {
    "upsert_episode": "upsert_episode",
    "upsert_fact": "upsert_fact",
    "delete_student": "delete_student",
}
MAX_ERROR_LENGTH = 500


class OutboxWorker:
    """Deliver one bounded batch while retaining its delivery diagnostics.

    ``processed_total`` and the return value count claimed/attempted rows,
    including rows that enter retry or dead-letter state. ``successful_total``
    counts only rows whose outbox completion committed successfully. The
    failure fields describe only the most recent batch; ``last_errors`` is
    keyed by outbox ID.
    """

    def __init__(
        self,
        connection: psycopg.Connection,
        index: Any | None = None,
        *,
        batch_size: int = 20,
        lease_seconds: int = CLAIM_LEASE_SECONDS,
    ) -> None:
        self.outbox = OutboxRepository(connection)
        self.index = index
        self.batch_size = batch_size
        self.lease_seconds = lease_seconds
        self.processed_total = 0
        self.successful_total = 0
        self.failed_total = 0
        self.last_errors: dict[str, str] = {}

    def run_pending(
        self,
        *,
        now: str | None = None,
        student_id: str | None = None,
        outbox_ids: Sequence[str] | None = None,
    ) -> int:
        """Process one batch synchronously; return the number of rows claimed."""
        self._reset_run_state()
        if self.index is None:
            return 0
        processed = 0
        timestamp, lease_deadline = self._batch_times(now)
        processed_students: set[str] = set()
        while processed < self.batch_size:
            selected_student_id = self.outbox.due_student_id(
                now=timestamp,
                student_id=student_id,
                outbox_ids=outbox_ids,
                exclude_student_ids=processed_students,
            )
            if selected_student_id is None:
                break
            processed_students.add(selected_student_id)
            with student_advisory_lock(self.outbox.connection, selected_student_id):
                rows = self.outbox.claim_due(
                    now=timestamp,
                    batch_size=self.batch_size - processed,
                    lease_deadline=lease_deadline,
                    student_id=selected_student_id,
                    outbox_ids=outbox_ids,
                )
                if not rows:
                    continue
                asyncio.run(self._deliver_rows(rows, timestamp))
                processed += len(rows)
        self.processed_total += processed
        return processed

    async def run_pending_async(
        self,
        *,
        now: str | None = None,
        student_id: str | None = None,
        outbox_ids: Sequence[str] | None = None,
    ) -> int:
        self._reset_run_state()
        if self.index is None:
            return 0
        processed = 0
        timestamp, lease_deadline = self._batch_times(now)
        processed_students: set[str] = set()
        while processed < self.batch_size:
            selected_student_id = self.outbox.due_student_id(
                now=timestamp,
                student_id=student_id,
                outbox_ids=outbox_ids,
                exclude_student_ids=processed_students,
            )
            if selected_student_id is None:
                break
            processed_students.add(selected_student_id)
            async with student_advisory_lock_async(
                self.outbox.connection, selected_student_id
            ):
                rows = self.outbox.claim_due(
                    now=timestamp,
                    batch_size=self.batch_size - processed,
                    lease_deadline=lease_deadline,
                    student_id=selected_student_id,
                    outbox_ids=outbox_ids,
                )
                if not rows:
                    continue
                await self._deliver_rows(rows, timestamp)
                processed += len(rows)
        self.processed_total += processed
        return processed

    def _reset_run_state(self) -> None:
        self.successful_total = 0
        self.failed_total = 0
        self.last_errors = {}

    def _batch_times(self, now: str | None) -> tuple[str, str]:
        timestamp = now or utc_now_iso()
        lease_deadline = (
            datetime.fromisoformat(timestamp) + timedelta(seconds=self.lease_seconds)
        ).isoformat()
        return timestamp, lease_deadline

    async def _deliver_rows(self, rows: list, timestamp: str) -> None:
        for row in rows:
            method_name = DISPATCH_BY_OPERATION.get(row.operation)
            if method_name is None or not hasattr(self.index, method_name):
                error = f"unsupported operation {row.operation}"[:MAX_ERROR_LENGTH]
                new_status = self.outbox.mark_failed(
                    row.outbox_id, row.claim_token, error, now=timestamp
                )
                self._record_failure(row.outbox_id, new_status, error)
                continue

            if method_name in ("upsert_episode", "upsert_fact"):
                payload_student_id = row.payload.get("student_id")
                if payload_student_id != row.student_id:
                    error = (
                        f"payload student_id {payload_student_id!r} does not match "
                        f"authoritative student_id {row.student_id!r}"
                    )[:MAX_ERROR_LENGTH]
                    new_status = self.outbox.mark_failed(
                        row.outbox_id, row.claim_token, error, now=timestamp
                    )
                    self._record_failure(row.outbox_id, new_status, error)
                    continue

            try:
                method = getattr(self.index, method_name)
                if method_name in ("upsert_episode", "upsert_fact"):
                    await method(row.payload, row.idempotency_key)
                else:
                    await method(row.student_id, row.idempotency_key)
                if method_name == "delete_student":
                    completed = self.outbox.mark_deleted(
                        row.outbox_id, row.claim_token, now=timestamp
                    )
                else:
                    completed = self.outbox.complete(
                        row.outbox_id, row.claim_token, now=timestamp
                    )
                if not completed:
                    self._record_stale(row.outbox_id)
                    continue
                self.successful_total += 1
            except Exception as exc:  # index outage -> retry schedule -> dead letter
                error = str(exc)[:MAX_ERROR_LENGTH]
                new_status = self.outbox.mark_failed(
                    row.outbox_id, row.claim_token, error, now=timestamp
                )
                self._record_failure(row.outbox_id, new_status, error)

    def _record_stale(self, outbox_id: str) -> None:
        """A transition lost to a newer claim; diagnostic only, no counters."""
        self.last_errors[outbox_id] = "stale claim; superseded by another worker"

    def _record_failure(
        self, outbox_id: str, new_status: str | None, error: str
    ) -> None:
        if new_status is None:
            self._record_stale(outbox_id)
            return
        self.failed_total += 1
        self.last_errors[outbox_id] = error
