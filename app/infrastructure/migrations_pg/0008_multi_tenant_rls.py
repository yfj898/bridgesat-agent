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
    _grant_app_role(connection)
    _create_token_resolver(connection)


def _grant_app_role(connection: psycopg.Connection) -> None:
    """Grant runtime privileges to bridgesat_app.

    bridgesat (superuser) owns all tables and bypasses RLS; bridgesat_app is
    the RLS subject. Default privileges make future tables usable without
    re-granting.
    """
    connection.execute("GRANT USAGE ON SCHEMA public TO bridgesat_app")
    connection.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO bridgesat_app"
    )
    connection.execute(
        "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO bridgesat_app"
    )
    connection.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO bridgesat_app"
    )
    connection.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        "GRANT USAGE, SELECT ON SEQUENCES TO bridgesat_app"
    )


def _create_token_resolver(connection: psycopg.Connection) -> None:
    """Token resolution runs BEFORE app.tenant_id is set, so it must bypass
    RLS. SECURITY DEFINER (owner = bridgesat superuser) + exact hash match
    exposes only the row for the presented token."""
    connection.execute(
        """
        CREATE OR REPLACE FUNCTION public.resolve_token(p_hash TEXT)
        RETURNS TABLE (tenant_id TEXT, student_id TEXT)
        LANGUAGE sql
        SECURITY DEFINER
        SET search_path = pg_catalog, public, pg_temp
        AS $$
            SELECT tenant_id, student_id FROM public.student_tokens
            WHERE token_hash = p_hash AND revoked_at IS NULL
        $$
        """
    )
    connection.execute(
        "REVOKE ALL ON FUNCTION public.resolve_token(TEXT) FROM PUBLIC"
    )
    connection.execute(
        "GRANT EXECUTE ON FUNCTION public.resolve_token(TEXT) TO bridgesat_app"
    )
