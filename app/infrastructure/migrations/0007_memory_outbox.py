"""0007: memory_outbox.

Transactional outbox for asynchronous derived-index delivery (Mnemis) and
student-deletion bookkeeping, per MEMORY_CONSISTENCY spec sections 3.4, 4, 5
and 11. SQLite remains the authoritative store; outbox rows are delivery
intent, never authoritative facts.
"""

from __future__ import annotations

import sqlite3


def migrate(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS memory_outbox (
            outbox_id TEXT PRIMARY KEY,
            student_id TEXT NOT NULL,
            aggregate_type TEXT NOT NULL,
            aggregate_id TEXT NOT NULL,
            operation TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            next_attempt_at TEXT NOT NULL,
            last_error TEXT,
            created_at TEXT NOT NULL,
            completed_at TEXT,
            FOREIGN KEY (student_id) REFERENCES students(id)
        );

        CREATE INDEX IF NOT EXISTS idx_memory_outbox_due
            ON memory_outbox (status, next_attempt_at);

        CREATE TABLE IF NOT EXISTS student_deletions (
            student_id TEXT PRIMARY KEY,
            state TEXT NOT NULL,
            requested_at TEXT NOT NULL,
            sqlite_deleted_at TEXT,
            index_deletion_pending_at TEXT,
            verified_at TEXT,
            last_error TEXT,
            FOREIGN KEY (student_id) REFERENCES students(id)
        );
        """
    )
