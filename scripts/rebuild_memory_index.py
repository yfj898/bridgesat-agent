#!/usr/bin/env python3
"""Rebuild the derived memory index from the authoritative SQLite store.

Usage:
    python scripts/rebuild_memory_index.py [--db PATH] [--student STUDENT_ID]
                                           [--index stub|adapter]

Rebuild is per-student: delete the student's indexed memories, re-enqueue
upsert operations for every validated episode and evidenced fact, then run
the outbox worker. Idempotency keys keep repeated rebuilds duplicate-free.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.infrastructure.database import connect, transaction
from app.infrastructure import migration_runner
from app.infrastructure.learner_store import LearnerStore  # noqa: F401  (documents the data model)
from app.memory.mnemis_stub import InMemoryMnemisIndex
from app.memory.outbox import OutboxRepository
from app.memory.worker import OutboxWorker

DEFAULT_DB = ROOT / "data" / "bridgesat.db"


def _enqueue_episodes(connection, student_id: str, repo: OutboxRepository) -> int:
    rows = connection.execute(
        """
        SELECT * FROM learning_episodes
        WHERE student_id = ? AND status = 'validated'
        ORDER BY created_at
        """,
        (student_id,),
    ).fetchall()
    count = 0
    for row in rows:
        repo.enqueue(
            connection,
            student_id=student_id,
            aggregate_type="episode",
            aggregate_id=row["episode_id"],
            operation="upsert_episode",
            payload=dict(row),
            version=1,
        )
        count += 1
    return count


def _enqueue_facts(connection, student_id: str, repo: OutboxRepository) -> int:
    rows = connection.execute(
        "SELECT * FROM student_memory_facts WHERE student_id = ?", (student_id,)
    ).fetchall()
    count = 0
    for row in rows:
        repo.enqueue(
            connection,
            student_id=student_id,
            aggregate_type="fact",
            aggregate_id=row["fact_id"],
            operation="upsert_fact",
            payload=dict(row),
            version=row["version"],
        )
        count += 1
    return count


def rebuild_student(database_path: Path, student_id: str, index) -> dict:
    with connect(database_path) as connection:
        with transaction(connection):
            # Rebuild is deterministic: wipe this student's delivery rows,
            # then enqueue delete-first and upserts-after, so the worker
            # processes them in that order.
            connection.execute(
                "DELETE FROM memory_outbox WHERE student_id = ?", (student_id,)
            )
            repo = OutboxRepository(database_path)
            repo.enqueue(
                connection,
                student_id=student_id,
                aggregate_type="student",
                aggregate_id=student_id,
                operation="delete_student",
                payload={"student_id": student_id},
                version=1,
            )
            episodes = _enqueue_episodes(connection, student_id, repo)
            facts = _enqueue_facts(connection, student_id, repo)
    worker = OutboxWorker(database_path, index=index)
    worker.run_pending()
    return {"student_id": student_id, "episodes_enqueued": episodes, "facts_enqueued": facts}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--student", default=None, help="rebuild one student only")
    parser.add_argument(
        "--index", default="stub", choices=["stub", "adapter"], help="index backend"
    )
    args = parser.parse_args()

    db = args.db or Path("data/bridgesat.db")
    if not db.is_file():
        print(f"Database {db} not found", file=sys.stderr)
        return 1
    migration_runner.apply_migrations(db)

    index = InMemoryMnemisIndex() if args.index == "stub" else None
    if index is None:
        from app.memory.mnemis_backend import MnemisMemoryAdapter

        index = MnemisMemoryAdapter()

    with connect(db) as connection:
        students = (
            [args.student]
            if args.student
            else [r["id"] for r in connection.execute("SELECT id FROM students").fetchall()]
        )

    report = {"db": str(db), "students": []}
    for student_id in students:
        report["students"].append(rebuild_student(db, student_id, index))
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
