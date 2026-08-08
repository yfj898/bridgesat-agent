"""0006: sync protocol (PG). Device registry, session branch tracking, and
offline/online sync conflict records."""
from __future__ import annotations

import psycopg


def migrate(connection: psycopg.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS devices (
            device_id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL DEFAULT 'tenant_demo',
            student_id TEXT NOT NULL,
            device_name TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            last_seen_at TEXT,
            revoked_at TEXT
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS session_branches (
            branch_id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL DEFAULT 'tenant_demo',
            session_id TEXT NOT NULL,
            parent_session_id TEXT,
            device_id TEXT NOT NULL,
            branched_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open'
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS sync_conflicts (
            conflict_id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL DEFAULT 'tenant_demo',
            session_id TEXT NOT NULL,
            device_id TEXT NOT NULL,
            event_id TEXT NOT NULL,
            resolution TEXT NOT NULL,
            resolved_at TEXT NOT NULL
        )
        """
    )
    for table in ("devices", "session_branches", "sync_conflicts"):
        connection.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{table}_tenant ON {table} (tenant_id)"
        )
