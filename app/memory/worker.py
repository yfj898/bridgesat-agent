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
from typing import Any

import psycopg

from .outbox import CLAIM_LEASE_SECONDS, OutboxRepository, utc_now_iso

DISPATCH_BY_OPERATION = {
    "upsert_episode": "upsert_episode",
    "upsert_fact": "upsert_fact",
    "delete_student": "delete_student",
}


class OutboxWorker:
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

    def run_pending(self, *, now: str | None = None) -> int:
        """Process one batch synchronously; returns the number processed."""
        return asyncio.run(self.run_pending_async(now=now))

    async def run_pending_async(self, *, now: str | None = None) -> int:
        if self.index is None:
            return 0
        processed = 0
        timestamp = now or utc_now_iso()
        lease_deadline = (
            datetime.fromisoformat(timestamp) + timedelta(seconds=self.lease_seconds)
        ).isoformat()
        rows = self.outbox.claim_due(
            now=timestamp, batch_size=self.batch_size, lease_deadline=lease_deadline
        )
        for row in rows:
            processed += 1
            method_name = DISPATCH_BY_OPERATION.get(row.operation)
            if method_name is None or not hasattr(self.index, method_name):
                self.outbox.mark_failed(
                    row.outbox_id, f"unsupported operation {row.operation}", now=timestamp
                )
                continue
            try:
                method = getattr(self.index, method_name)
                if method_name in ("upsert_episode", "upsert_fact"):
                    await method(row.payload, row.idempotency_key)
                else:
                    await method(row.payload.get("student_id"), row.idempotency_key)
                self.outbox.complete(row.outbox_id, now=timestamp)
            except Exception as exc:  # index outage -> retry schedule -> dead letter
                self.outbox.mark_failed(row.outbox_id, str(exc)[:500], now=timestamp)
        self.processed_total += processed
        return processed