"""0003: learning session core (PG). All student-scoped tables carry
tenant_id; RLS is enabled by migration 0008."""
from __future__ import annotations

import psycopg


def migrate(connection: psycopg.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS students (
            id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL DEFAULT 'tenant_demo',
            name TEXT NOT NULL,
            daily_minutes INTEGER NOT NULL,
            target_score INTEGER NOT NULL,
            mastery_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT ''
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_students_tenant ON students (tenant_id)
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS student_tokens (
            token_id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL DEFAULT 'tenant_demo',
            student_id TEXT NOT NULL,
            token_hash TEXT NOT NULL UNIQUE,
            device_bound_name TEXT,
            created_at TEXT NOT NULL,
            revoked_at TEXT
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS student_skill_states (
            student_id TEXT NOT NULL,
            tenant_id TEXT NOT NULL DEFAULT 'tenant_demo',
            skill TEXT NOT NULL,
            alpha DOUBLE PRECISION NOT NULL,
            beta DOUBLE PRECISION NOT NULL,
            mastery DOUBLE PRECISION NOT NULL,
            confidence DOUBLE PRECISION NOT NULL,
            evidence_count INTEGER NOT NULL DEFAULT 0,
            correct_streak INTEGER NOT NULL DEFAULT 0,
            incorrect_streak INTEGER NOT NULL DEFAULT 0,
            last_practiced_at TEXT,
            review_due_at TEXT,
            projection_origin TEXT NOT NULL DEFAULT 'live',
            updated_at TEXT NOT NULL,
            PRIMARY KEY (student_id, skill)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS study_plans (
            plan_id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL DEFAULT 'tenant_demo',
            student_id TEXT NOT NULL,
            session_id TEXT,
            plan_json TEXT NOT NULL,
            policy_version TEXT NOT NULL,
            created_at TEXT NOT NULL,
            superseded_by_plan_id TEXT
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS study_sessions (
            session_id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL DEFAULT 'tenant_demo',
            student_id TEXT NOT NULL,
            session_state TEXT NOT NULL,
            paused_from_state TEXT,
            started_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS session_items (
            session_id TEXT NOT NULL,
            tenant_id TEXT NOT NULL DEFAULT 'tenant_demo',
            sequence INTEGER NOT NULL,
            content_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            skill TEXT NOT NULL,
            subskill TEXT,
            difficulty INTEGER NOT NULL,
            role TEXT NOT NULL,
            shown_at TEXT,
            answered_at TEXT,
            PRIMARY KEY (session_id, sequence)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS answer_attempts (
            attempt_id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL DEFAULT 'tenant_demo',
            event_id TEXT NOT NULL UNIQUE,
            student_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            content_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            sequence INTEGER NOT NULL,
            selected_choice_id TEXT NOT NULL,
            correct INTEGER NOT NULL,
            hint_level INTEGER NOT NULL DEFAULT 0,
            weight DOUBLE PRECISION NOT NULL DEFAULT 0.0,
            validity TEXT NOT NULL DEFAULT 'valid',
            occurred_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS learning_events (
            event_id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL DEFAULT 'tenant_demo',
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
            integrity_hash TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_events (
            event_id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL DEFAULT 'tenant_demo',
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
            created_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS misconception_evidence (
            evidence_id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL DEFAULT 'tenant_demo',
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
            observed_at TEXT NOT NULL
        )
        """
    )
    for table in (
        "student_tokens",
        "student_skill_states",
        "study_plans",
        "study_sessions",
        "session_items",
        "answer_attempts",
        "learning_events",
        "agent_events",
        "misconception_evidence",
    ):
        connection.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{table}_tenant ON {table} (tenant_id)"
        )
