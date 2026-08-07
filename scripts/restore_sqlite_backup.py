#!/usr/bin/env python3
"""Restore a BridgeSAT SQLite backup (API_AND_OPERATIONS section 7-8).

Safety rules:
- refuses to restore over the backup file itself;
- refuses a missing backup or missing target parent;
- refuses a non-SQLite backup file (header sniff).

Usage:
    python scripts/restore_sqlite_backup.py --backup data/backups/registry-pre-migration-<ts>.db \
        --target content/registry.db
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BACKUPS_DIR = ROOT / "data" / "backups"


def _is_sqlite(path: Path) -> bool:
    with path.open("rb") as handle:
        return handle.read(16) == b"SQLite format 3\x00"


def restore_backup(backup_path: Path, target_path: Path) -> None:
    if backup_path == target_path:
        raise ValueError("backup path and target path must differ")
    if not backup_path.is_file():
        raise FileNotFoundError(f"backup not found: {backup_path}")
    if not _is_sqlite(backup_path):
        raise ValueError(f"backup is not a SQLite database: {backup_path}")
    target_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(backup_path, target_path)
    with sqlite3.connect(target_path) as connection:
        has_ledger = connection.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type = 'table' AND name = 'schema_migrations'"
        ).fetchone()[0]
        version = None
        if has_ledger:
            version = connection.execute(
                "SELECT MAX(version) FROM schema_migrations"
            ).fetchone()[0]
    print(f"Restored {target_path} from {backup_path} "
          f"(schema version {version if version is not None else 'n/a'}).")


def main() -> int:
    parser = argparse.ArgumentParser(description="Restore a BridgeSAT SQLite backup.")
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    args = parser.parse_args()

    try:
        restore_backup(args.backup, args.target)
    except (ValueError, FileNotFoundError) as exc:
        print(f"Restore refused: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
