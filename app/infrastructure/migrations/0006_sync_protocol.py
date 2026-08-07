"""0006: sync_protocol.

Adds device registration, session branch tracking, and sync conflict audit
tables required by the offline synchronization protocol (SYNC_PROTOCOL.md):

- `devices`: revocable device registrations with per-device sequence cursors;
- `session_branches`: primary/parallel branch designation per device/session;
- `sync_conflicts`: append-only audit rows for semantic conflicts
  (PARALLEL_ATTEMPT_DETECTED, SESSION_BRANCH_CONFLICT, ...).

Immutable `learning_events` already exists (0003); sync processing appends to
the same table and applies projections in server receive order.
"""

from __future__ import annotations

import sqlite3


def migrate(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS devices (
            device_id TEXT PRIMARY KEY,
            student_id TEXT NOT NULL,
            device_name TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            last_device_sequence INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            revoked_at TEXT,
            FOREIGN KEY (student_id) REFERENCES students(id)
        );

        CREATE TABLE IF NOT EXISTS session_branches (
            branch_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            device_id TEXT NOT NULL,
            branch_state TEXT NOT NULL DEFAULT 'primary',
            base_snapshot_version INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            PRIMARY KEY (branch_id, session_id),
            FOREIGN KEY (session_id) REFERENCES study_sessions(session_id)
        );

        CREATE TABLE IF NOT EXISTS sync_conflicts (
            conflict_id TEXT PRIMARY KEY,
            event_id TEXT NOT NULL,
            student_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            conflict_type TEXT NOT NULL,
            detail_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (event_id) REFERENCES learning_events(event_id)
        );

        CREATE INDEX IF NOT EXISTS idx_sync_conflicts_student
            ON sync_conflicts (student_id, created_at);
        """
    )
