from __future__ import annotations

import importlib.util
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from .database import MIGRATION_DIR, transaction

SCHEMA_VERSION = 7


class UnsupportedDatabaseError(RuntimeError):
    pass


class MigrationError(RuntimeError):
    pass


def default_backups_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "data" / "backups"


def create_backup(database_path: Path, *, backups_dir: Path | None = None) -> Path | None:
    """Copy the database file before destructive migration (API_AND_OPERATIONS §7-8).

    The backup lives outside the active database path, named with the source
    stem and a timestamp so restore points are unambiguous. Returns None when
    there is nothing to back up (no file, or no pending migrations).
    """
    if not database_path.is_file():
        return None
    backups_dir = backups_dir or default_backups_dir()
    backups_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
    backup_path = backups_dir / f"{database_path.stem}-pre-migration-{stamp}.db"
    shutil.copy2(database_path, backup_path)
    return backup_path


def _load_migration(path: Path):
    module_name = f"app.infrastructure.migrations.{path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise MigrationError(f"Cannot load migration {path.name}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _available_migrations() -> list[tuple[int, str, Path]]:
    migrations: list[tuple[int, str, Path]] = []
    for path in sorted(MIGRATION_DIR.glob("[0-9][0-9][0-9][0-9]_*.py")):
        version = int(path.stem.split("_", 1)[0])
        migrations.append((version, path.stem, path))
    return migrations


def apply_migrations(
    database_path: Path,
    *,
    allow_rollback: bool = True,
    backups_dir: Path | None = None,
) -> int:
    database_path = Path(database_path)
    db_existed = database_path.is_file()
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at TEXT NOT NULL
            )
            """
        )
        applied = {
            row["version"] for row in connection.execute("SELECT version FROM schema_migrations")
        }
        target = SCHEMA_VERSION
        if applied and max(applied) > target:
            raise UnsupportedDatabaseError(
                f"Database schema version {max(applied)} is newer than supported "
                f"version {target}"
            )

        pending = [m for m in _available_migrations() if m[0] not in applied and m[0] <= target]
        if pending and db_existed:
            create_backup(database_path, backups_dir=backups_dir)

        for version, name, path in pending:
            module = _load_migration(path)
            with transaction(connection):
                module.migrate(connection)
                connection.execute(
                    "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                    (version, name, datetime.now(UTC).isoformat()),
                )
                applied.add(version)
        return max(applied) if applied else 0
    finally:
        connection.close()
