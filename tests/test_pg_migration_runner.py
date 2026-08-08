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
    conn = pg.connect()
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
