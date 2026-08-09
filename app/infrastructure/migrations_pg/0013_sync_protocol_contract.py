"""0013: sync protocol contract (PG). Align the device registry and
conflict log with the shapes SyncService writes:

- devices gained `status` and `last_device_sequence` (device lifecycle and
  the sequence-increase rule live in the service);
- sync_conflicts was rebuilt to the conflict record contract
  (conflict_id, event_id, student_id, session_id, conflict_type,
  detail_json, created_at) that SyncService inserts; the 0006 table used a
  different shape (device_id/resolution/resolved_at).

Rebuilding sync_conflicts drops its RLS policy, so tenant isolation is
re-applied here (same policy shape as migration 0008).
"""
from __future__ import annotations

import psycopg


def migrate(connection: psycopg.Connection) -> None:
    _extend_devices(connection)
    _rebuild_sync_conflicts(connection)


def _extend_devices(connection: psycopg.Connection) -> None:
    connection.execute(
        "ALTER TABLE devices ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'active'"
    )
    connection.execute(
        "ALTER TABLE devices ADD COLUMN IF NOT EXISTS "
        "last_device_sequence INTEGER NOT NULL DEFAULT 0"
    )


def _rebuild_sync_conflicts(connection: psycopg.Connection) -> None:
    connection.execute("DROP TABLE IF EXISTS sync_conflicts")
    connection.execute(
        """
        CREATE TABLE sync_conflicts (
            conflict_id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL DEFAULT 'tenant_demo',
            event_id TEXT NOT NULL,
            student_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            conflict_type TEXT NOT NULL,
            detail_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_sync_conflicts_tenant "
        "ON sync_conflicts (tenant_id)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_sync_conflicts_student "
        "ON sync_conflicts (student_id, created_at)"
    )
    connection.execute("ALTER TABLE sync_conflicts ENABLE ROW LEVEL SECURITY")
    connection.execute(
        """
        CREATE POLICY tenant_isolation ON sync_conflicts
        USING (tenant_id = current_setting('app.tenant_id', true))
        """
    )