"""0008: multi-tenant row-level security.

Enables RLS on every tenant-scoped table with a single policy: the row's
tenant_id must equal the session variable app.tenant_id. Application code
sets `SET LOCAL app.tenant_id = '<tenant>'` inside each request transaction
(see app/main.py tenant middleware). Content tables stay unprotected.
"""
from __future__ import annotations

import psycopg

TENANT_TABLES = (
    "students", "student_tokens", "student_skill_states", "study_plans",
    "study_sessions", "session_items", "answer_attempts", "learning_events",
    "agent_events", "misconception_evidence", "learning_episodes",
    "student_memory_facts", "intervention_stats", "devices",
    "session_branches", "sync_conflicts", "memory_outbox", "student_deletions",
)


def migrate(connection: psycopg.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS tenant_roles (
            tenant_id TEXT PRIMARY KEY,
            role_name TEXT NOT NULL DEFAULT 'tenant_member'
        )
        """
    )
    for table in TENANT_TABLES:
        connection.execute(
            f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"
        )
        connection.execute(
            f"DROP POLICY IF EXISTS tenant_isolation ON {table}"
        )
        connection.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table}
            USING (tenant_id = current_setting('app.tenant_id', true))
            """
        )
