"""PostgreSQL migration runner.

Migrations live in app/infrastructure/migrations_pg/ as 000N_*.py modules
exporting `migrate(connection: psycopg.Connection) -> None`. Each migration
runs in its own transaction; schema_migrations records every applied version.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import psycopg

from . import pg
from .pg import transaction

MIGRATION_DIR = Path(__file__).resolve().parent / "migrations_pg"

SCHEMA_VERSION = 13


class UnsupportedDatabaseError(RuntimeError):
    pass


class MigrationError(RuntimeError):
    pass


def _load_migration(path: Path) -> ModuleType:
    module_name = f"app.infrastructure.migrations_pg.{path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise MigrationError(f"Cannot load migration {path.name}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not hasattr(module, "migrate"):
        raise MigrationError(f"Migration {path.name} does not export migrate()")
    return module


def _available_migrations() -> list[tuple[int, str, Path]]:
    migrations: list[tuple[int, str, Path]] = []
    for path in sorted(MIGRATION_DIR.glob("[0-9][0-9][0-9][0-9]_*.py")):
        version = int(path.stem.split("_", 1)[0])
        migrations.append((version, path.stem, path))
    return migrations


def migrate_database(connection: psycopg.Connection) -> int:
    """Apply all pending migrations and return the resulting schema version."""
    connection.execute("SELECT pg_advisory_lock(hashtext('bridgesat_migrations'))")
    try:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        applied = {
            row["version"]
            for row in connection.execute("SELECT version FROM schema_migrations").fetchall()
        }
        target = SCHEMA_VERSION
        if applied and max(applied) > target:
            raise UnsupportedDatabaseError(
                f"Database schema version {max(applied)} is newer than supported "
                f"version {target}"
            )
        pending = [m for m in _available_migrations() if m[0] not in applied and m[0] <= target]
        for version, name, path in pending:
            module = _load_migration(path)
            with transaction(connection):
                module.migrate(connection)
                connection.execute(
                    "INSERT INTO schema_migrations (version, name, applied_at) "
                    "VALUES (%s, %s, now())",
                    (version, name),
                )
                applied.add(version)
        return max(applied) if applied else 0
    finally:
        connection.commit()
        connection.execute("SELECT pg_advisory_unlock(hashtext('bridgesat_migrations'))")


def apply_migrations(target: str | None = None) -> int:
    """Open a superuser connection (migrations need DDL/GRANT rights),
    migrate, close. Returns version."""
    connection = pg.connect_admin(target)
    try:
        return migrate_database(connection)
    finally:
        connection.close()
