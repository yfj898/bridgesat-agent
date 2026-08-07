#!/usr/bin/env python3
"""Verify index parity against the authoritative SQLite store.

Usage:
    python scripts/verify_memory_parity.py [--db PATH] [--student STUDENT_ID]

The indexed state is derived, not persistent here, so parity is verified the
way MEMORY_CONSISTENCY §12 defines it: a rebuild from SQLite into a fresh
index must reproduce the expected episodes and facts for every student.
Exits 1 when parity fails, so it can gate release.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.infrastructure.database import connect
from app.infrastructure import migration_runner
from app.memory.mnemis_stub import InMemoryMnemisIndex
from app.memory.outbox import OutboxRepository
from app.memory.worker import OutboxWorker
from scripts.rebuild_memory_index import rebuild_student

DEFAULT_DB = ROOT / "data" / "bridgesat.db"


def _sqlite_counts(connection, student_id: str) -> dict:
    episodes = connection.execute(
        """
        SELECT COUNT(*) AS c FROM learning_episodes
        WHERE student_id = ? AND status = 'validated'
        """,
        (student_id,),
    ).fetchone()["c"]
    facts = connection.execute(
        "SELECT COUNT(*) AS c FROM student_memory_facts WHERE student_id = ?",
        (student_id,),
    ).fetchone()["c"]
    return {"episodes": episodes, "facts": facts}


async def _index_counts(index, student_id: str) -> dict:
    episodes = await index.count_episodes(student_id)
    facts = await index.count_facts(student_id)
    return {"episodes": episodes, "facts": facts}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--student", default=None)
    args = parser.parse_args()

    db = args.db or DEFAULT_DB
    if not db.is_file():
        print(f"Database {db} not found", file=sys.stderr)
        return 1
    migration_runner.apply_migrations(db)

    with connect(db) as connection:
        students = (
            [args.student]
            if args.student
            else [r["id"] for r in connection.execute("SELECT id FROM students").fetchall()]
        )

    index = InMemoryMnemisIndex()
    rows = []
    ok = True
    for student_id in students:
        rebuild_student(db, student_id, index)
        with connect(db) as connection:
            sqlite = _sqlite_counts(connection, student_id)
        indexed = asyncio.run(_index_counts(index, student_id))
        match = sqlite == indexed
        ok = ok and match
        rows.append(
            {
                "student_id": student_id,
                "sqlite": sqlite,
                "indexed": indexed,
                "parity": "ok" if match else "MISMATCH",
            }
        )

    report = {
        "parity": "ok" if ok else "MISMATCH",
        "students": rows,
        "outbox": OutboxRepository(db).consistency_metrics(),
    }
    print(json.dumps(report, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
