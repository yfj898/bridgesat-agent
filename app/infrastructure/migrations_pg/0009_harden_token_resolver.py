"""0009: harden the SECURITY DEFINER token resolver for existing databases."""
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
