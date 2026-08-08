"""PG 迁移器测试(替代原 SQLite 版 test_migrations 语义)。"""
from __future__ import annotations

import pytest

from app.infrastructure import pg
from app.infrastructure.migration_runner import (
    MigrationError,
    SCHEMA_VERSION,
    UnsupportedDatabaseError,
    apply_migrations,
    migrate_database,
)


@pytest.fixture()
def database():
    conn = pg.connect_admin()
    migrate_database(conn)
    yield conn
    conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    conn.commit()
    conn.close()


def test_fresh_database_migrates_to_supported_version(database) -> None:
    assert pg.database_version(database) == SCHEMA_VERSION


def test_migrations_are_idempotent(database) -> None:
    migrate_database(database)  # second run
    assert pg.database_version(database) == SCHEMA_VERSION


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
