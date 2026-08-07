import json
import sqlite3
from pathlib import Path

import pytest

from app.infrastructure import migration_runner
from app.infrastructure.database import connect, database_version


def _schema_tables(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    return {row[0] for row in rows}


def test_fresh_database_migrates_to_supported_version(tmp_path: Path) -> None:
    db = tmp_path / "fresh.db"
    version = migration_runner.apply_migrations(db)
    assert version == migration_runner.SCHEMA_VERSION

    with connect(db) as connection:
        tables = _schema_tables(connection)
        required = {
            "schema_migrations",
            "legacy_mastery_imports",
            "skills",
            "skill_prerequisites",
            "content_sources",
            "content_items",
            "content_item_versions",
            "content_reviews",
            "content_packs",
            "content_pack_items",
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
            "knowledge_index_log",
            "knowledge_fts",
            "memory_outbox",
            "student_deletions",
        }
        assert required <= tables
        assert database_version(connection) == migration_runner.SCHEMA_VERSION


def test_idempotent_migrations(tmp_path: Path) -> None:
    db = tmp_path / "idem.db"
    migration_runner.apply_migrations(db)
    migration_runner.apply_migrations(db)
    with connect(db) as connection:
        assert database_version(connection) == migration_runner.SCHEMA_VERSION
        count = connection.execute(
            "SELECT COUNT(*) FROM schema_migrations"
        ).fetchone()[0]
        assert count == migration_runner.SCHEMA_VERSION


def test_legacy_student_data_preserved_and_projected(tmp_path: Path) -> None:
    db = tmp_path / "legacy.db"
    connection = sqlite3.connect(db)
    connection.execute(
        """
        CREATE TABLE students (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            daily_minutes INTEGER NOT NULL,
            target_score INTEGER NOT NULL,
            mastery_json TEXT NOT NULL
        )
        """
    )
    connection.execute(
        "INSERT INTO students VALUES (?, ?, ?, ?, ?)",
        (
            "legacy-1",
            "Ari",
            20,
            1200,
            json.dumps(
                {
                    "linear_equations": 0.3,
                    "ratios": 0.7,
                    "reading_inference": 0.9,
                    "unknown_skill": 0.5,
                }
            ),
        ),
    )
    connection.commit()
    connection.close()

    migration_runner.apply_migrations(db)

    with connect(db) as connection:
        row = connection.execute(
            "SELECT id, name, daily_minutes, target_score, status FROM students"
        ).fetchone()
        assert row["id"] == "legacy-1"
        assert row["status"] == "active"

        projected = {
            r["skill"]: (r["mastery"], r["projection_origin"])
            for r in connection.execute(
                "SELECT skill, mastery, projection_origin FROM student_skill_states"
            ).fetchall()
        }
        assert "linear_equations" in projected
        assert projected["linear_equations"][0] == pytest.approx(0.3, abs=0.01)
        assert projected["linear_equations"][1] == "legacy_import"
        assert "ratios_percentages" in projected
        assert "reading_inference" not in projected
        assert "unknown_skill" not in projected


def test_duplicate_migration_application_does_not_duplicate_rows(tmp_path: Path) -> None:
    db = tmp_path / "dup.db"
    connection = sqlite3.connect(db)
    connection.execute(
        """
        CREATE TABLE students (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            daily_minutes INTEGER NOT NULL,
            target_score INTEGER NOT NULL,
            mastery_json TEXT NOT NULL
        )
        """
    )
    connection.execute(
        "INSERT INTO students VALUES ('s1', 'N', 20, 1200, '{}')"
    )
    connection.commit()
    connection.close()

    migration_runner.apply_migrations(db)
    migration_runner.apply_migrations(db)
    with connect(db) as connection:
        rows = connection.execute("SELECT student_id FROM legacy_mastery_imports").fetchall()
        assert len(rows) == 1


def test_newer_database_schema_is_rejected(tmp_path: Path) -> None:
    db = tmp_path / "newer.db"
    migration_runner.apply_migrations(db)
    connection = sqlite3.connect(db)
    connection.execute(
        "INSERT INTO schema_migrations (version, name, applied_at) VALUES (99, 'future', '2026-01-01')"
    )
    connection.commit()
    connection.close()

    with pytest.raises(migration_runner.UnsupportedDatabaseError):
        migration_runner.apply_migrations(db)


def test_pre_migration_backup_created_before_pending_migrations(tmp_path: Path) -> None:
    db = tmp_path / "old.db"
    connection = sqlite3.connect(db)
    connection.execute(
        """
        CREATE TABLE students (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            daily_minutes INTEGER NOT NULL,
            target_score INTEGER NOT NULL,
            mastery_json TEXT NOT NULL
        )
        """
    )
    connection.execute("INSERT INTO students VALUES ('s1', 'N', 20, 1200, '{}')")
    connection.commit()
    connection.close()

    backups = tmp_path / "backups"
    migration_runner.apply_migrations(db, backups_dir=backups)

    backup_files = sorted(backups.glob("old-pre-migration-*.db"))
    assert len(backup_files) == 1
    with sqlite3.connect(backup_files[0]) as backup_connection:
        tables = _schema_tables(backup_connection)
        assert "students" in tables
        row = backup_connection.execute("SELECT name FROM students").fetchone()
        assert row[0] == "N"


def test_no_backup_for_fresh_database(tmp_path: Path) -> None:
    db = tmp_path / "fresh.db"
    backups = tmp_path / "backups"
    migration_runner.apply_migrations(db, backups_dir=backups)
    assert list(backups.glob("*.db")) == []


def test_idempotent_run_creates_no_second_backup(tmp_path: Path) -> None:
    db = tmp_path / "once.db"
    connection = sqlite3.connect(db)
    connection.execute("CREATE TABLE students (id TEXT PRIMARY KEY)")
    connection.commit()
    connection.close()

    backups = tmp_path / "backups"
    migration_runner.apply_migrations(db, backups_dir=backups)
    migration_runner.apply_migrations(db, backups_dir=backups)
    assert len(list(backups.glob("*.db"))) == 1


def test_restore_backup_round_trip(tmp_path: Path) -> None:
    from scripts.restore_sqlite_backup import restore_backup

    original = tmp_path / "live.db"
    connection = sqlite3.connect(original)
    connection.execute("CREATE TABLE payload (id TEXT PRIMARY KEY, value TEXT)")
    connection.execute("INSERT INTO payload VALUES ('a', 'kept')")
    connection.commit()
    connection.close()

    backups = tmp_path / "backups"
    backup = migration_runner.create_backup(original, backups_dir=backups)
    assert backup is not None

    with sqlite3.connect(original) as connection:
        connection.execute("DROP TABLE payload")

    restore_backup(backup, original)
    with sqlite3.connect(original) as connection:
        row = connection.execute("SELECT value FROM payload WHERE id = 'a'").fetchone()
        assert row[0] == "kept"


def test_restore_refuses_same_path_missing_and_non_sqlite(tmp_path: Path) -> None:
    from scripts.restore_sqlite_backup import restore_backup

    backup = tmp_path / "backup.db"
    backup.write_bytes(b"not a database")

    with pytest.raises(ValueError, match="must differ"):
        restore_backup(backup, backup)
    with pytest.raises(FileNotFoundError):
        restore_backup(tmp_path / "missing.db", tmp_path / "target.db")
    with pytest.raises(ValueError, match="not a SQLite database"):
        restore_backup(backup, tmp_path / "target.db")
