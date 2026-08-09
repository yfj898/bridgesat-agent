"""0011: align memory_outbox schema with the domain outbox contract.

0007 created memory_outbox with a minimal column set (state/next_retry_at/
updated_at); the OutboxRepository contract requires the SQLite-era columns
(aggregate_type/aggregate_id/operation/idempotency_key/status/next_attempt_at/
completed_at). This migration renames state -> status, next_retry_at ->
next_attempt_at, drops the NOT NULL updated_at column (unused by the repo),
and adds the missing delivery columns plus supporting indexes. The
(tenant_id, idempotency_key) unique index keeps idempotent enqueue semantics
per tenant.
"""
from __future__ import annotations

import psycopg


def migrate(connection: psycopg.Connection) -> None:
    connection.execute(
        "ALTER TABLE memory_outbox RENAME COLUMN state TO status"
    )
    connection.execute(
        "ALTER TABLE memory_outbox RENAME COLUMN next_retry_at TO next_attempt_at"
    )
    connection.execute(
        "ALTER TABLE memory_outbox DROP COLUMN IF EXISTS updated_at"
    )
    connection.execute(
        "ALTER TABLE memory_outbox "
        "ADD COLUMN IF NOT EXISTS aggregate_type TEXT NOT NULL DEFAULT ''"
    )
    connection.execute(
        "ALTER TABLE memory_outbox "
        "ADD COLUMN IF NOT EXISTS aggregate_id TEXT NOT NULL DEFAULT ''"
    )
    connection.execute(
        "ALTER TABLE memory_outbox "
        "ADD COLUMN IF NOT EXISTS operation TEXT NOT NULL DEFAULT ''"
    )
    connection.execute(
        "ALTER TABLE memory_outbox "
        "ADD COLUMN IF NOT EXISTS idempotency_key TEXT NOT NULL DEFAULT ''"
    )
    connection.execute(
        "ALTER TABLE memory_outbox "
        "ADD COLUMN IF NOT EXISTS completed_at TEXT"
    )
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_memory_outbox_idempotency "
        "ON memory_outbox (tenant_id, idempotency_key)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_memory_outbox_due "
        "ON memory_outbox (status, next_attempt_at)"
    )