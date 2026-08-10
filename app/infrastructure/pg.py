"""PostgreSQL connection layer for BridgeSAT.

All storage modules get connections from here; nothing else talks to the
database directly. Connections use psycopg3 with dict-row access and the
public schema by default.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg.rows import dict_row

DEFAULT_ADMIN_DSN = "postgresql://bridgesat:bridgesat@localhost:5432/bridgesat"
DEFAULT_APP_DSN = "postgresql://bridgesat_app:bridgesat@localhost:5432/bridgesat"
TENANT_TABLES = (
    "students",
    "student_tokens",
    "student_skill_states",
    "study_plans",
    "study_sessions",
    "session_items",
    "answer_attempts",
    "learning_events",
    "agent_events",
    "misconception_evidence",
    "learning_episodes",
    "student_memory_facts",
    "intervention_stats",
    "devices",
    "session_branches",
    "sync_conflicts",
    "memory_outbox",
    "student_deletions",
    "legacy_mastery_imports",
)


def admin_dsn() -> str:
    return os.getenv("BRIDGESAT_ADMIN_DB", DEFAULT_ADMIN_DSN)


def dsn() -> str:
    """Application-role DSN (RLS applies). Runtime and tests use this."""
    return os.getenv("BRIDGESAT_DB", DEFAULT_APP_DSN)


def connect_admin(target: str | None = None) -> psycopg.Connection:
    """Superuser connection: migrations/DDL/GRANT only."""
    return psycopg.connect(
        target or admin_dsn(),
        row_factory=dict_row,
        autocommit=False,
        options="-c search_path=public",
    )


def connect(target: str | None = None) -> psycopg.Connection:
    """Application-role connection with dict-row access. Caller closes it."""
    connection = psycopg.connect(
        target or dsn(),
        row_factory=dict_row,
        autocommit=False,
        options="-c search_path=public",
    )
    try:
        assert_safe_app_role(connection)
        connection.rollback()
    except BaseException:
        quiet_close(connection)
        raise
    return connection


def database_identity(connection: psycopg.Connection) -> tuple[object, ...]:
    """Return the server/database identity for a live PostgreSQL connection."""
    row = connection.execute(
        """
        SELECT current_database() AS database,
               COALESCE(inet_server_addr()::text, 'local') AS host,
               COALESCE(inet_server_port(), 0) AS port,
               pg_postmaster_start_time() AS postmaster_start_time
        """
    ).fetchone()
    return (
        row["database"],
        row["host"],
        row["port"],
        row["postmaster_start_time"],
    )


def assert_matching_database(
    admin: psycopg.Connection, app: psycopg.Connection
) -> None:
    """Reject admin/application connections that resolve to different targets."""
    if database_identity(admin) != database_identity(app):
        raise RuntimeError(
            "PostgreSQL admin and application targets resolve to different "
            "PostgreSQL database targets"
        )


def assert_safe_app_role(connection: psycopg.Connection) -> None:
    """Reject effective roles that can bypass tenant isolation."""
    row = connection.execute(
        """
        SELECT r.rolsuper,
               r.rolbypassrls,
               EXISTS (
                   SELECT 1
                   FROM pg_class AS c
                   JOIN pg_namespace AS n ON n.oid = c.relnamespace
                   WHERE n.nspname = 'public'
                     AND c.relname = ANY(%s)
                     AND (
                         pg_has_role(current_user, c.relowner, 'USAGE')
                         OR pg_has_role(current_user, c.relowner, 'SET')
                     )
               ) AS owns_tenant_table
        FROM pg_roles AS r
        WHERE r.rolname = current_user
        """,
        (list(TENANT_TABLES),),
    ).fetchone()
    if (
        row is None
        or row["rolsuper"]
        or row["rolbypassrls"]
        or row["owns_tenant_table"]
    ):
        raise RuntimeError(
            "PostgreSQL application connection requires a non-superuser, "
            "non-RLS-bypass, non-owner role"
        )


def quiet_close(connection: psycopg.Connection | None) -> None:
    """Rollback and close without masking an earlier failure."""
    if connection is None:
        return
    try:
        connection.rollback()
    except BaseException:
        pass
    try:
        connection.close()
    except BaseException:
        pass


@contextmanager
def transaction(connection: psycopg.Connection) -> Iterator[psycopg.Connection]:
    """Commit on success, rollback and re-raise on any exception.

    psycopg3 opens a transaction implicitly on first execute; we only need
    explicit commit/rollback control.
    """
    try:
        yield connection
        connection.commit()
    except BaseException:
        try:
            connection.rollback()
        except BaseException:
            pass
        raise


def database_version(connection: psycopg.Connection) -> int:
    """Return the highest applied migration version, or 0 if none exist.

    The table may not exist on an unmigrated database; a direct SELECT would
    raise UndefinedTable and leave the connection in an aborted transaction,
    so we check existence first with ``to_regclass``.
    """
    exists = connection.execute(
        "SELECT to_regclass('public.schema_migrations') AS relation"
    ).fetchone()["relation"]
    if exists is None:
        return 0
    row = connection.execute(
        "SELECT MAX(version) AS version FROM schema_migrations"
    ).fetchone()
    return int(row["version"]) if row and row["version"] is not None else 0
