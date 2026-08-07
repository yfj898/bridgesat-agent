"""0001: bootstrap_legacy_students.

Preserves pre-migration `students` data (created by the original skeleton
repository) and records an auditable initial projection of the legacy mastery
payload so nothing is silently lost. The legacy table itself is left untouched;
0003 extends students and seeds student_skill_states from this snapshot.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

# Only skills within the competition math scope are projected; reading skills
# are extension scope and intentionally excluded from the MVP projection.
LEGACY_SKILL_MAP = {
    "linear_equations": "linear_equations",
    "ratios": "ratios_percentages",
}


def migrate(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS legacy_mastery_imports (
            student_id TEXT PRIMARY KEY,
            mastery_json TEXT NOT NULL,
            imported_at TEXT NOT NULL
        )
        """
    )
    table_exists = connection.execute(
        """
        SELECT 1 FROM sqlite_master
        WHERE type = 'table' AND name = 'students'
        """
    ).fetchone()
    if table_exists is None:
        return
    columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(students)")
    }
    if "mastery_json" not in columns or "id" not in columns:
        return
    rows = connection.execute("SELECT id, mastery_json FROM students").fetchall()
    imported_at = datetime.now(UTC).isoformat()
    for row in rows:
        mastery_json = row["mastery_json"]
        try:
            payload = json.loads(mastery_json)
        except json.JSONDecodeError:
            payload = {}
        normalized = {}
        for raw_skill, value in payload.items():
            mapped = LEGACY_SKILL_MAP.get(raw_skill)
            if mapped is not None and isinstance(value, (int, float)):
                normalized[mapped] = float(value)
        connection.execute(
            """
            INSERT INTO legacy_mastery_imports (student_id, mastery_json, imported_at)
            VALUES (?, ?, ?)
            ON CONFLICT(student_id) DO UPDATE SET
                mastery_json = excluded.mastery_json,
                imported_at = excluded.imported_at
            """,
            (row["id"], json.dumps(normalized, sort_keys=True), imported_at),
        )
