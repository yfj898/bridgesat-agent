"""0001: legacy mastery imports (PG)."""
from __future__ import annotations

import psycopg


def migrate(connection: psycopg.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS legacy_mastery_imports (
            import_id TEXT PRIMARY KEY,
            student_id TEXT NOT NULL,
            mastery_json TEXT NOT NULL,
            imported_at TEXT NOT NULL
        )
        """
    )
