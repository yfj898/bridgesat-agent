#!/usr/bin/env python3
"""Replay dead-letter outbox records after fixing the root cause.

Usage:
    python scripts/replay_dead_letter.py [--db PATH] [--max-attempts N]

Resets dead_letter rows to pending with a fresh attempt budget and runs the
worker once per attempt window, honoring the retry schedule.
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
from app.memory.mnemis_stub import InMemoryMnemisIndex
from app.memory.worker import OutboxWorker

DEFAULT_DB = ROOT / "data" / "bridgesat.db"


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
    worker = OutboxWorker(db, index=index)
    total = 0
    for _ in range(max_attempts):
        total += worker.run_pending()
    return {"reset_rows": len(rows), "processed": total}


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
    report = replay(db, InMemoryMnemisIndex(), args.max_attempts)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
