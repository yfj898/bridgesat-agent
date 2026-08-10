#!/usr/bin/env python3
"""Import a built content pack into the PostgreSQL content registry.

Usage:
    python scripts/import_content_pack.py [--db DSN] [--admin-db DSN]
                                          [--pack PATH]

Publishing runs on the admin connection (the app role has SELECT only on the
shared content tables); the tsvector index is rebuilt on the same admin
connection; verification runs on a clean app-role connection.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.content_pipeline.contracts import PACKS_DIR
from app.content_pipeline.importing import import_pack, verify_import
from app.infrastructure import pg
from app.infrastructure.migration_runner import migrate_database
from app.knowledge.local_backend import index_pack


def _default_pack() -> Path:
    candidates = sorted(PACKS_DIR.glob("bridgesat-math-*"), reverse=True)
    if not candidates:
        print("No built pack found under content/packs/", file=sys.stderr)
        sys.exit(1)
    return candidates[0]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=None, help="PostgreSQL application DSN")
    parser.add_argument("--admin-db", default=None, help="PostgreSQL admin DSN")
    parser.add_argument("--pack", type=Path, default=None)
    args = parser.parse_args()

    pack = args.pack or _default_pack()
    target = args.db or pg.dsn()
    admin_target = args.admin_db or pg.admin_dsn()

    admin = None
    connection = None
    try:
        admin = pg.connect_admin(admin_target)
        connection = pg.connect(target)
        pg.assert_safe_app_role(connection)
        try:
            pg.assert_matching_database(admin, connection)
        except RuntimeError as exc:
            print(f"Refusing to run: {exc}", file=sys.stderr)
            return 2
        admin.rollback()
        connection.rollback()
        migrate_database(admin)
        inserted = import_pack(admin, pack)
        indexed = index_pack(admin, pack)
        summary = verify_import(connection)
    finally:
        pg.quiet_close(connection)
        pg.quiet_close(admin)

    print(f"Imported {inserted} items from {pack.name} into {target}")
    print(f"Registry: {summary}")
    print(f"Indexed: {indexed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
