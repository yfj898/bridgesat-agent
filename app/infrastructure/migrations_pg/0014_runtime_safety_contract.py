"""0014: runtime safety and legacy tenant-isolation contract."""
from __future__ import annotations

import psycopg


def migrate(connection: psycopg.Connection) -> None:
    connection.execute(
        """
        CREATE OR REPLACE FUNCTION public.resolve_token(p_hash TEXT)
        RETURNS TABLE (tenant_id TEXT, student_id TEXT)
        LANGUAGE sql
        SECURITY DEFINER
        SET search_path = pg_catalog, public, pg_temp
        AS $$
            SELECT tokens.tenant_id, tokens.student_id
            FROM public.student_tokens AS tokens
            JOIN public.students AS students
              ON students.id = tokens.student_id
             AND students.tenant_id = tokens.tenant_id
            WHERE tokens.token_hash = p_hash
              AND tokens.revoked_at IS NULL
              AND students.status = 'active'
        $$
        """
    )
    connection.execute(
        "REVOKE ALL ON FUNCTION public.resolve_token(TEXT) FROM PUBLIC"
    )
    connection.execute(
        "GRANT EXECUTE ON FUNCTION public.resolve_token(TEXT) TO bridgesat_app"
    )

    connection.execute(
        "ALTER TABLE memory_outbox "
        "ADD COLUMN IF NOT EXISTS claim_token TEXT"
    )
    connection.execute(
        "UPDATE memory_outbox "
        "SET status = 'pending', claim_token = NULL "
        "WHERE status = 'processing'"
    )

    connection.execute(
        "ALTER TABLE legacy_mastery_imports "
        "ADD COLUMN IF NOT EXISTS tenant_id TEXT"
    )
    connection.execute(
        """
        UPDATE legacy_mastery_imports AS legacy
        SET tenant_id = students.tenant_id
        FROM students
        WHERE students.id = legacy.student_id
          AND legacy.tenant_id IS NULL
        """
    )
    connection.execute(
        "UPDATE legacy_mastery_imports "
        "SET tenant_id = 'tenant_demo' "
        "WHERE tenant_id IS NULL"
    )
    connection.execute(
        "ALTER TABLE legacy_mastery_imports "
        "ALTER COLUMN tenant_id SET DEFAULT 'tenant_demo', "
        "ALTER COLUMN tenant_id SET NOT NULL"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_legacy_mastery_imports_tenant "
        "ON legacy_mastery_imports (tenant_id)"
    )
    connection.execute(
        "ALTER TABLE legacy_mastery_imports ENABLE ROW LEVEL SECURITY"
    )
    connection.execute(
        "DROP POLICY IF EXISTS tenant_isolation ON legacy_mastery_imports"
    )
    connection.execute(
        """
        CREATE POLICY tenant_isolation ON legacy_mastery_imports
        USING (tenant_id = current_setting('app.tenant_id', true))
        """
    )
    connection.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE "
        "ON TABLE legacy_mastery_imports TO bridgesat_app"
    )
