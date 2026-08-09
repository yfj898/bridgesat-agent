"""0012: student_deletions contract aligned with the deletion protocol
(MEMORY_CONSISTENCY §11).

The original 0007 PG table used a `status`/`completed_at` shape that does
not match the deletion state machine (requested -> sqlite_deleted ->
index_deletion_pending -> verified | failed) nor the SQLite-era columns.
This migration renames `status` to `state`, adds the per-state timestamp
tracking columns the service writes, keeps `deletion_id` as the primary key
(SQLite-era student_id primary key is replaced by a unique index on
student_id), and drops the unused `completed_at`.
"""
from __future__ import annotations

import psycopg


def migrate(connection: psycopg.Connection) -> None:
    connection.execute(
        "ALTER TABLE student_deletions RENAME COLUMN status TO state"
    )
    for column in ("sqlite_deleted_at", "index_deletion_pending_at", "verified_at", "last_error"):
        connection.execute(
            f"ALTER TABLE student_deletions ADD COLUMN IF NOT EXISTS {column} TEXT"
        )
    connection.execute(
        "ALTER TABLE student_deletions DROP COLUMN IF EXISTS completed_at"
    )
    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_student_deletions_student_id
        ON student_deletions (student_id)
        """
    )