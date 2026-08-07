"""0003: learning_session_core.

Extends `students` with lifecycle fields, adds token/skill-state/plan/session
tables, and creates the immutable event log (`learning_events`, `agent_events`)
plus answer attempts and misconception evidence. Legacy mastery snapshots
recorded by 0001 are projected into student_skill_states here, inside the same
migration transaction.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

DEFAULT_ALPHA = 2.0
DEFAULT_BETA = 2.0


def migrate(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS students (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            daily_minutes INTEGER NOT NULL,
            target_score INTEGER NOT NULL,
            mastery_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS student_tokens (
            token_id TEXT PRIMARY KEY,
            student_id TEXT NOT NULL,
            token_hash TEXT NOT NULL UNIQUE,
            device_bound_name TEXT,
            created_at TEXT NOT NULL,
            revoked_at TEXT,
            FOREIGN KEY (student_id) REFERENCES students(id)
        );

        CREATE TABLE IF NOT EXISTS student_skill_states (
            student_id TEXT NOT NULL,
            skill TEXT NOT NULL,
            alpha REAL NOT NULL,
            beta REAL NOT NULL,
            mastery REAL NOT NULL,
            confidence REAL NOT NULL,
            evidence_count INTEGER NOT NULL DEFAULT 0,
            correct_streak INTEGER NOT NULL DEFAULT 0,
            incorrect_streak INTEGER NOT NULL DEFAULT 0,
            last_practiced_at TEXT,
            review_due_at TEXT,
            projection_origin TEXT NOT NULL DEFAULT 'live',
            updated_at TEXT NOT NULL,
            PRIMARY KEY (student_id, skill),
            FOREIGN KEY (student_id) REFERENCES students(id)
        );

        CREATE TABLE IF NOT EXISTS study_plans (
            plan_id TEXT PRIMARY KEY,
            student_id TEXT NOT NULL,
            session_id TEXT,
            plan_json TEXT NOT NULL,
            policy_version TEXT NOT NULL,
            created_at TEXT NOT NULL,
            superseded_by_plan_id TEXT,
            FOREIGN KEY (student_id) REFERENCES students(id)
        );

        CREATE TABLE IF NOT EXISTS study_sessions (
            session_id TEXT PRIMARY KEY,
            student_id TEXT NOT NULL,
            session_state TEXT NOT NULL,
            paused_from_state TEXT,
            started_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT,
            FOREIGN KEY (student_id) REFERENCES students(id)
        );

        CREATE TABLE IF NOT EXISTS session_items (
            session_id TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            content_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            skill TEXT NOT NULL,
            subskill TEXT,
            difficulty INTEGER NOT NULL,
            role TEXT NOT NULL,
            shown_at TEXT,
            answered_at TEXT,
            PRIMARY KEY (session_id, sequence),
            FOREIGN KEY (session_id) REFERENCES study_sessions(session_id)
        );

        CREATE TABLE IF NOT EXISTS answer_attempts (
            attempt_id TEXT PRIMARY KEY,
            event_id TEXT NOT NULL UNIQUE,
            student_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            content_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            sequence INTEGER NOT NULL,
            selected_choice_id TEXT NOT NULL,
            correct INTEGER NOT NULL,
            hint_level INTEGER NOT NULL DEFAULT 0,
            weight REAL NOT NULL DEFAULT 0.0,
            validity TEXT NOT NULL DEFAULT 'valid',
            occurred_at TEXT NOT NULL,
            FOREIGN KEY (student_id) REFERENCES students(id),
            FOREIGN KEY (session_id) REFERENCES study_sessions(session_id)
        );

        CREATE TABLE IF NOT EXISTS learning_events (
            event_id TEXT PRIMARY KEY,
            student_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            policy_version TEXT NOT NULL,
            content_version TEXT,
            occurred_at TEXT NOT NULL,
            received_at TEXT NOT NULL,
            device_id TEXT,
            device_sequence INTEGER,
            origin TEXT NOT NULL,
            integrity_hash TEXT NOT NULL,
            FOREIGN KEY (student_id) REFERENCES students(id)
        );

        CREATE TABLE IF NOT EXISTS agent_events (
            event_id TEXT PRIMARY KEY,
            student_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            source_event_id TEXT,
            state_before TEXT,
            state_after TEXT,
            action TEXT NOT NULL,
            action_payload_json TEXT NOT NULL,
            reason_code TEXT NOT NULL,
            reason_text TEXT NOT NULL,
            policy_version TEXT NOT NULL,
            taxonomy_version TEXT,
            content_version TEXT,
            referenced_content_json TEXT,
            episode_ids_json TEXT,
            source TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (student_id) REFERENCES students(id)
        );

        CREATE TABLE IF NOT EXISTS misconception_evidence (
            evidence_id TEXT PRIMARY KEY,
            student_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            event_id TEXT NOT NULL,
            skill TEXT NOT NULL,
            subskill TEXT,
            misconception TEXT NOT NULL,
            source_label TEXT NOT NULL,
            confidence_label TEXT NOT NULL,
            state TEXT NOT NULL,
            item_id TEXT NOT NULL,
            item_version INTEGER NOT NULL,
            observed_at TEXT NOT NULL,
            FOREIGN KEY (student_id) REFERENCES students(id)
        );
        """
    )

    _ensure_student_columns(connection)
    _project_legacy_mastery(connection)


def _ensure_student_columns(connection: sqlite3.Connection) -> None:
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(students)")}
    if "status" not in columns:
        connection.execute(
            "ALTER TABLE students ADD COLUMN status TEXT NOT NULL DEFAULT 'active'"
        )
    if "created_at" not in columns:
        connection.execute(
            "ALTER TABLE students ADD COLUMN created_at TEXT NOT NULL DEFAULT ''"
        )
    if "updated_at" not in columns:
        connection.execute(
            "ALTER TABLE students ADD COLUMN updated_at TEXT NOT NULL DEFAULT ''"
        )


def _project_legacy_mastery(connection: sqlite3.Connection) -> None:
    imports = connection.execute("SELECT * FROM legacy_mastery_imports").fetchall()
    now = datetime.now(UTC).isoformat()
    for row in imports:
        student_id = row["student_id"]
        try:
            mastery = json.loads(row["mastery_json"])
        except json.JSONDecodeError:
            mastery = {}
        for skill, value in mastery.items():
            if not isinstance(value, (int, float)):
                continue
            mastery_value = max(0.05, min(0.95, float(value)))
            total = DEFAULT_ALPHA + DEFAULT_BETA
            alpha = mastery_value * total
            beta = total - alpha
            connection.execute(
                """
                INSERT INTO student_skill_states (
                    student_id, skill, alpha, beta, mastery, confidence,
                    evidence_count, correct_streak, incorrect_streak,
                    last_practiced_at, review_due_at, projection_origin, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 0, 0, 0, NULL, NULL, 'legacy_import', ?)
                ON CONFLICT(student_id, skill) DO NOTHING
                """,
                (
                    student_id,
                    skill,
                    round(alpha, 4),
                    round(beta, 4),
                    round(mastery_value, 4),
                    0.0,
                    now,
                ),
            )
