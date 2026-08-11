"""PG 迁移器测试(替代原 SQLite 版 test_migrations 语义)。"""
from __future__ import annotations

import uuid

import psycopg
import pytest

from app.infrastructure import pg
from app.infrastructure.migration_runner import (
    MigrationError,
    SCHEMA_VERSION,
    UnsupportedDatabaseError,
    apply_migrations,
    migrate_database,
)


MISCONCEPTION_EVIDENCE_INDEX = "idx_misconception_evidence_lookup"


def _has_misconception_evidence_index(connection) -> bool:
    row = connection.execute(
        "SELECT 1 FROM pg_catalog.pg_indexes "
        "WHERE schemaname = 'public' AND indexname = %s",
        (MISCONCEPTION_EVIDENCE_INDEX,),
    ).fetchone()
    return row is not None


def _columns(connection, table_name: str) -> set[str]:
    return {
        row["column_name"]
        for row in connection.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = %s",
            (table_name,),
        ).fetchall()
    }


def _has_policy(connection, table_name: str, policy_name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM pg_policy "
        "WHERE polrelid = %s::regclass AND polname = %s",
        (f"public.{table_name}", policy_name),
    ).fetchone()
    return row is not None


@pytest.fixture()
def database():
    conn = pg.connect_admin()
    migrate_database(conn)
    yield conn
    conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    conn.commit()
    conn.close()


@pytest.fixture()
def isolated_database():
    database_name = f"bridgesat_test_{uuid.uuid4().hex}"
    maintenance_dsn = psycopg.conninfo.make_conninfo(
        pg.admin_dsn(), dbname="postgres"
    )
    admin_dsn = psycopg.conninfo.make_conninfo(
        pg.admin_dsn(), dbname=database_name
    )
    app_dsn = psycopg.conninfo.make_conninfo(pg.dsn(), dbname=database_name)

    maintenance = psycopg.connect(maintenance_dsn, autocommit=True)
    try:
        maintenance.execute(
            psycopg.sql.SQL("CREATE DATABASE {}").format(
                psycopg.sql.Identifier(database_name)
            )
        )
    finally:
        maintenance.close()

    try:
        yield admin_dsn, app_dsn
    finally:
        maintenance = psycopg.connect(maintenance_dsn, autocommit=True)
        try:
            maintenance.execute(
                psycopg.sql.SQL("DROP DATABASE {} WITH (FORCE)").format(
                    psycopg.sql.Identifier(database_name)
                )
            )
        finally:
            maintenance.close()


def test_fresh_database_migrates_to_supported_version(database) -> None:
    assert pg.database_version(database) == SCHEMA_VERSION


def test_fresh_database_has_runtime_safety_contract(database) -> None:
    assert SCHEMA_VERSION == 16
    assert pg.database_version(database) == 16
    assert "claim_token" in _columns(database, "memory_outbox")
    assert "tenant_id" in _columns(database, "legacy_mastery_imports")

    legacy_rls = database.execute(
        "SELECT relrowsecurity FROM pg_class "
        "WHERE oid = 'public.legacy_mastery_imports'::regclass"
    ).fetchone()
    assert legacy_rls["relrowsecurity"] is True
    assert _has_policy(database, "legacy_mastery_imports", "tenant_isolation")


def test_fresh_database_has_hybrid_decision_trace_contract(database) -> None:
    assert SCHEMA_VERSION == 16
    columns = _columns(database, "hybrid_decision_trace")
    assert {
        "trace_id",
        "tenant_id",
        "student_id",
        "source_event_id",
        "decision_token",
        "fallback_action",
        "verified_action",
        "accepted_checks",
        "created_at",
    } <= columns
    assert database.execute(
        "SELECT relrowsecurity FROM pg_class "
        "WHERE oid = 'public.hybrid_decision_trace'::regclass"
    ).fetchone()["relrowsecurity"] is True
    assert _has_policy(database, "hybrid_decision_trace", "tenant_isolation")


def test_migration_0014_normalizes_existing_processing_rows(
    monkeypatch, isolated_database
) -> None:
    from app.infrastructure import migration_runner as runner

    admin_dsn, _ = isolated_database
    connection = pg.connect_admin(admin_dsn)
    try:
        monkeypatch.setattr(runner, "SCHEMA_VERSION", 13)
        assert runner.migrate_database(connection) == 13
        connection.execute(
            "INSERT INTO memory_outbox "
            "(outbox_id, tenant_id, student_id, payload_json, status, "
            "attempt_count, next_attempt_at, last_error, created_at, "
            "aggregate_type, aggregate_id, operation, idempotency_key) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                "outbox_processing",
                "tenant_demo",
                "student_demo",
                "{}",
                "processing",
                1,
                "2026-01-01T00:00:00+00:00",
                "stale worker",
                "2026-01-01T00:00:00+00:00",
                "episode",
                "episode-1",
                "upsert",
                "processing-row",
            ),
        )
        connection.commit()

        monkeypatch.setattr(runner, "SCHEMA_VERSION", 14)
        assert runner.migrate_database(connection) == 14
        row = connection.execute(
            "SELECT status, claim_token FROM memory_outbox "
            "WHERE outbox_id = %s",
            ("outbox_processing",),
        ).fetchone()
        assert row == {"status": "pending", "claim_token": None}
    finally:
        connection.close()


def test_migration_0014_backfills_legacy_tenant_and_grants_runtime_access(
    monkeypatch, isolated_database
) -> None:
    from app.infrastructure import migration_runner as runner

    admin_dsn, _ = isolated_database
    connection = pg.connect_admin(admin_dsn)
    try:
        monkeypatch.setattr(runner, "SCHEMA_VERSION", 13)
        assert runner.migrate_database(connection) == 13
        connection.execute(
            "INSERT INTO students "
            "(id, tenant_id, name, daily_minutes, target_score, mastery_json) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            ("student_known", "tenant_known", "Known", 30, 90, "{}"),
        )
        connection.execute(
            "INSERT INTO legacy_mastery_imports "
            "(import_id, student_id, mastery_json, imported_at) "
            "VALUES (%s, %s, %s, %s), (%s, %s, %s, %s)",
            (
                "known-import",
                "student_known",
                "{}",
                "2026-01-01",
                "orphan-import",
                "student_missing",
                "{}",
                "2026-01-01",
            ),
        )
        connection.commit()

        monkeypatch.setattr(runner, "SCHEMA_VERSION", 14)
        assert runner.migrate_database(connection) == 14

        rows = connection.execute(
            "SELECT import_id, tenant_id FROM legacy_mastery_imports "
            "ORDER BY import_id"
        ).fetchall()
        assert rows == [
            {"import_id": "known-import", "tenant_id": "tenant_known"},
            {"import_id": "orphan-import", "tenant_id": "tenant_demo"},
        ]

        column = connection.execute(
            "SELECT is_nullable, column_default "
            "FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = %s "
            "AND column_name = 'tenant_id'",
            ("legacy_mastery_imports",),
        ).fetchone()
        assert column["is_nullable"] == "NO"
        assert "tenant_demo" in column["column_default"]

        index = connection.execute(
            "SELECT 1 FROM pg_indexes "
            "WHERE schemaname = 'public' "
            "AND indexname = 'idx_legacy_mastery_imports_tenant'"
        ).fetchone()
        assert index is not None

        assert all(
            connection.execute(
                "SELECT has_table_privilege(%s, %s, %s) AS allowed",
                ("bridgesat_app", "public.legacy_mastery_imports", privilege),
            ).fetchone()["allowed"]
            for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE")
        )
    finally:
        connection.close()


def test_fresh_database_has_content_registry_contract(database) -> None:
    shared = {
        "content_sources": {"source_name", "source_type", "redistribution_allowed",
                            "rag_ingestion_allowed", "access_method", "attribution",
                            "maintenance_status", "last_verified_at"},
        "content_items": {"schema_version", "domain", "stable_version", "status",
                          "license_snapshot_json", "source_lineage_json",
                          "canonical_body_hash", "created_at", "withdrawn_at",
                          "withdrawn_reason"},
        "content_item_versions": {"item_json", "content_hash", "created_at"},
        "content_reviews": {"version", "reviewer_role", "reviewer_id", "conclusion",
                            "notes", "release_batch"},
        "content_packs": {"manifest_json"},
        "content_pack_items": {"version"},
    }
    for table, columns in shared.items():
        assert columns <= _columns(database, table), table

    for table in (
        "skills",
        "skill_prerequisites",
        "content_sources",
        "content_items",
        "content_item_versions",
        "content_reviews",
        "content_packs",
        "content_pack_items",
        "knowledge_fts",
    ):
        assert database.execute(
            "SELECT has_table_privilege('bridgesat_app', %s, 'SELECT') AS allowed",
            (f"public.{table}",),
        ).fetchone()["allowed"]
        assert not database.execute(
            "SELECT has_table_privilege('bridgesat_app', %s, 'INSERT') AS allowed",
            (f"public.{table}",),
        ).fetchone()["allowed"]
        assert not database.execute(
            "SELECT has_table_privilege('bridgesat_app', %s, 'UPDATE') AS allowed",
            (f"public.{table}",),
        ).fetchone()["allowed"]
        assert not database.execute(
            "SELECT has_table_privilege('bridgesat_app', %s, 'DELETE') AS allowed",
            (f"public.{table}",),
        ).fetchone()["allowed"]


def test_migrations_are_idempotent(database) -> None:
    migrate_database(database)  # second run
    assert pg.database_version(database) == SCHEMA_VERSION


def test_existing_v8_database_hardens_token_resolver(
    monkeypatch, isolated_database
) -> None:
    from app.infrastructure import migration_runner as runner

    admin_dsn, app_dsn = isolated_database
    connection = pg.connect_admin(admin_dsn)
    try:
        monkeypatch.setattr(runner, "SCHEMA_VERSION", 8)
        assert runner.migrate_database(connection) == 8

        connection.execute(
            """
            CREATE OR REPLACE FUNCTION resolve_token(p_hash TEXT)
            RETURNS TABLE (tenant_id TEXT, student_id TEXT)
            LANGUAGE sql
            SECURITY DEFINER
            SET search_path = public
            AS $$
                SELECT tenant_id, student_id FROM student_tokens
                WHERE token_hash = p_hash AND revoked_at IS NULL
            $$
            """
        )
        connection.execute(
            "INSERT INTO student_tokens "
            "(token_id, tenant_id, student_id, token_hash, created_at) "
            "VALUES (%s, %s, %s, %s, %s)",
            ("tok_real", "tenant_real", "stu_real", "realhash", "2026-01-01"),
        )
        connection.commit()

        connection.execute("CREATE SCHEMA resolver_hijack")
        connection.execute("SET search_path = resolver_hijack, public")
        monkeypatch.setattr(runner, "SCHEMA_VERSION", 9)
        assert runner.migrate_database(connection) == 9

        resolver = connection.execute(
            """
            SELECT p.prosecdef, p.proconfig,
                   EXISTS (
                       SELECT 1
                       FROM pg_catalog.aclexplode(p.proacl) AS acl
                       WHERE acl.grantee = 'bridgesat_app'::pg_catalog.regrole
                         AND acl.privilege_type = 'EXECUTE'
                   ) AS app_execute,
                   EXISTS (
                       SELECT 1
                       FROM pg_catalog.aclexplode(p.proacl) AS acl
                       WHERE acl.grantee = 0
                         AND acl.privilege_type = 'EXECUTE'
                   ) AS public_execute
            FROM pg_catalog.pg_proc AS p
            WHERE p.oid = 'public.resolve_token(text)'::pg_catalog.regprocedure
            """
        ).fetchone()
        assert resolver is not None
        assert resolver["prosecdef"] is True
        assert "search_path=pg_catalog, public, pg_temp" in resolver["proconfig"]
        assert resolver["app_execute"] is True
        assert resolver["public_execute"] is False

        app = pg.connect(app_dsn)
        try:
            app.execute(
                "CREATE TEMP TABLE student_tokens ("
                "tenant_id TEXT, student_id TEXT, token_hash TEXT, revoked_at TEXT"
                ")"
            )
            app.execute(
                "INSERT INTO student_tokens "
                "(tenant_id, student_id, token_hash, revoked_at) "
                "VALUES (%s, %s, %s, %s)",
                ("tenant_fake", "stu_fake", "realhash", None),
            )

            rows = app.execute("SELECT * FROM resolve_token('realhash')").fetchall()

            assert rows == [{"tenant_id": "tenant_real", "student_id": "stu_real"}]
        finally:
            app.close()
    finally:
        connection.close()


def test_fresh_database_creates_misconception_evidence_lookup_index(database) -> None:
    assert _has_misconception_evidence_index(database)


def test_existing_v9_database_gets_misconception_evidence_lookup_index(
    monkeypatch, isolated_database
) -> None:
    from app.infrastructure import migration_runner as runner

    admin_dsn, _ = isolated_database
    connection = pg.connect_admin(admin_dsn)
    try:
        monkeypatch.setattr(runner, "SCHEMA_VERSION", 9)
        assert runner.migrate_database(connection) == 9
        assert not _has_misconception_evidence_index(connection)

        monkeypatch.setattr(runner, "SCHEMA_VERSION", 10)
        assert runner.migrate_database(connection) == 10
        assert _has_misconception_evidence_index(connection)
    finally:
        connection.close()


def test_newer_database_schema_is_rejected(database) -> None:
    database.execute("INSERT INTO schema_migrations (version, name, applied_at) "
                     "VALUES (9999, 'future', now())")
    database.commit()
    with pytest.raises(UnsupportedDatabaseError):
        migrate_database(database)


def test_failing_migration_rolls_back_its_transaction(monkeypatch, tmp_path) -> None:
    from app.infrastructure import migration_runner as runner

    (tmp_path / "9001_ok.py").write_text(
        "import psycopg\n"
        "def migrate(connection: psycopg.Connection) -> None:\n"
        "    connection.execute('CREATE TABLE migration_probe_a (v INTEGER)')\n"
    )
    (tmp_path / "9002_bad.py").write_text(
        "import psycopg\n"
        "def migrate(connection: psycopg.Connection) -> None:\n"
        "    connection.execute('CREATE TABLE migration_probe_b (v INTEGER)')\n"
        "    raise RuntimeError('boom')\n"
    )
    monkeypatch.setattr(runner, "MIGRATION_DIR", tmp_path)
    monkeypatch.setattr(runner, "SCHEMA_VERSION", 9002)

    conn = pg.connect_admin()
    try:
        with pytest.raises(RuntimeError):
            runner.migrate_database(conn)
        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
        ).fetchall()
        names = {r["table_name"] for r in rows}
        assert "migration_probe_b" not in names
        assert "migration_probe_a" in names
        versions = {v["version"] for v in conn.execute("SELECT version FROM schema_migrations")}
        assert 9002 not in versions
        assert 9001 in versions
    finally:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
        conn.commit()
        conn.close()


def test_migrate_database_acquires_and_releases_advisory_lock(monkeypatch, tmp_path) -> None:
    from app.infrastructure import migration_runner as runner

    (tmp_path / "9001_ok.py").write_text(
        "import psycopg\n"
        "def migrate(connection: psycopg.Connection) -> None:\n"
        "    connection.execute('CREATE TABLE migration_probe_a (v INTEGER)')\n"
    )
    monkeypatch.setattr(runner, "MIGRATION_DIR", tmp_path)
    monkeypatch.setattr(runner, "SCHEMA_VERSION", 9001)

    conn = pg.connect_admin()
    try:
        assert runner.migrate_database(conn) == 9001
        assert conn.info.transaction_status is psycopg.pq.TransactionStatus.IDLE
        other = pg.connect()
        try:
            row = other.execute(
                "SELECT pg_try_advisory_lock(hashtext('bridgesat_migrations')) AS locked"
            ).fetchone()
            assert row["locked"] is True
        finally:
            other.execute("SELECT pg_advisory_unlock(hashtext('bridgesat_migrations'))")
            other.close()
    finally:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
        conn.commit()
        conn.close()


def test_required_tables_exist(database) -> None:
    required = {
        "students", "student_tokens", "student_skill_states", "study_sessions",
        "learning_events", "agent_events", "learning_episodes", "memory_outbox",
        "content_items", "knowledge_fts", "devices", "session_branches",
        "sync_conflicts", "student_deletions", "tenant_roles",
    }
    rows = database.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
    ).fetchall()
    names = {row["table_name"] for row in rows}
    missing = required - names
    assert not missing, f"missing tables: {missing}"
