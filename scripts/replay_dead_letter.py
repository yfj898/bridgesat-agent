#!/usr/bin/env python3
"""Replay dead-letter outbox records after fixing the root cause.

Usage:
    python scripts/replay_dead_letter.py [--db PATH] [--max-attempts N]

Resets dead_letter rows to pending with a fresh attempt budget, then runs the
worker once per attempt window, honoring the retry schedule.

Delivery uses the real configured index (mirroring app.main): in enhanced
mode that is the Mnemis adapter; otherwise the worker is left index=None and
rows stay pending for the running app to drain. It never marks rows indexed
against a throwaway in-memory stub, which would silently drop indexing.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.infrastructure import migration_runner
from app.infrastructure.database import connect, transaction
from app.memory import MemoryMode, memory_mode
from app.memory.worker import OutboxWorker

DEFAULT_DB = ROOT / "data" / "bridgesat.db"


def _make_index():
    if memory_mode() != MemoryMode.ENHANCED:
        return None
    from app.memory.mnemis_backend import MnemisMemoryAdapter

    return MnemisMemoryAdapter()


def replay(db: Path, index, max_attempts: int) -> dict:
    with connect(db) as connection:
        with transaction(connection):
            rows = connection.execute(
                "SELECT outbox_id FROM memory_outbox WHERE status = 'dead_letter'"
            ).fetchall()
            for row in rows:
                connection.execute(
                    """
                    UPDATE memory_outbox
                    SET status = 'pending', attempt_count = 0, next_attempt_at = ?,
                        last_error = NULL
                    WHERE outbox_id = ?
                    """,
                    (now_iso(), row["outbox_id"]),
                )
    processed = 0
    if index is not None:
        worker = OutboxWorker(db, index=index)
        for _ in range(max_attempts):
            processed += worker.run_pending()
    else:
        print(
            "Local memory mode: rows reset to pending; the app's own worker "
            "will deliver them on next startup.",
            file=sys.stderr,
        )
    return {"reset_rows": len(rows), "processed": processed}


def now_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--max-attempts", type=int, default=6)
    args = parser.parse_args()

    db = args.db or DEFAULT_DB
    if not db.is_file():
        print(f"Database {db} not found", file=sys.stderr)
        return 1
    migration_runner.apply_migrations(db)
    report = replay(db, _make_index(), args.max_attempts)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
