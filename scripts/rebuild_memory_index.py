#!/usr/bin/env python3
"""Rebuild the derived memory index from the authoritative PostgreSQL store.

Usage:
    python scripts/rebuild_memory_index.py [--db DSN] [--admin-db DSN]
                                           [--tenant TENANT_ID]
                                           [--student STUDENT_ID]
                                           [--index stub|adapter]

Rebuild is per-student: delete the student's indexed memories, re-enqueue
upsert operations for every validated episode and evidenced fact, then run
the outbox worker. Idempotency keys keep repeated rebuilds duplicate-free.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import psycopg

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
DATABASE_LABEL = "postgresql"

from app.infrastructure import pg
from app.infrastructure.migration_runner import migrate_database
from app.memory.mnemis_stub import InMemoryMnemisIndex
from app.memory.outbox import OutboxRepository, student_advisory_lock
from app.memory.worker import OutboxWorker


def _default_tenant() -> str:
    return os.getenv("BRIDGESAT_DEFAULT_TENANT", "tenant_demo")


def _enqueue_episodes(
    connection: psycopg.Connection, student_id: str, repo: OutboxRepository
) -> int:
    rows = connection.execute(
        """
        SELECT * FROM learning_episodes
        WHERE student_id = %s
          AND tenant_id = current_setting('app.tenant_id')
          AND status = 'validated'
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


def _enqueue_facts(
    connection: psycopg.Connection, student_id: str, repo: OutboxRepository
) -> int:
    rows = connection.execute(
        """
        SELECT * FROM student_memory_facts
        WHERE student_id = %s AND tenant_id = current_setting('app.tenant_id')
        """,
        (student_id,),
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


def _delivery_status_counts(
    connection: psycopg.Connection, student_id: str
) -> dict[str, int]:
    rows = connection.execute(
        "SELECT status, COUNT(*) AS total FROM memory_outbox "
        "WHERE student_id = %s AND tenant_id = current_setting('app.tenant_id') "
        "GROUP BY status",
        (student_id,),
    ).fetchall()
    return {row["status"]: int(row["total"]) for row in rows}


def _drain_worker(
    connection: psycopg.Connection,
    student_id: str,
    index: Any,
    *,
    max_batches: int | None = None,
) -> dict[str, Any]:
    worker = OutboxWorker(connection, index=index)
    claimed = 0
    successful = 0
    failed = 0
    failures: dict[str, str] = {}
    batches = 0
    while True:
        batch = worker.run_pending(student_id=student_id)
        batches += 1
        claimed += batch
        successful += worker.successful_total
        failed += worker.failed_total
        failures.update(worker.last_errors)
        if batch == 0 or (max_batches is not None and batches >= max_batches):
            break

    statuses = _delivery_status_counts(connection, student_id)
    return {
        "claimed": claimed,
        "successful": successful,
        "failed": failed,
        "failures": failures,
        "pending": statuses.get("pending", 0),
        "retrying": statuses.get("retrying", 0),
        "processing": statuses.get("processing", 0),
        "dead_letter": statuses.get("dead_letter", 0),
        "indexed": statuses.get("indexed", 0),
        "deleted": statuses.get("deleted", 0),
    }


def _merge_delivery_reports(*reports: dict[str, Any]) -> dict[str, Any]:
    attempt_keys = (
        "claimed",
        "successful",
        "failed",
    )
    status_keys = (
        "pending",
        "retrying",
        "processing",
        "dead_letter",
        "indexed",
        "deleted",
    )
    merged = {
        key: sum(int(report[key]) for report in reports) for key in attempt_keys
    }
    # Every report contains a snapshot of the same student's outbox. Status
    # snapshots must come from the final phase, not be added across phases.
    merged.update({key: int(reports[-1][key]) for key in status_keys})
    merged["failures"] = {
        key: value for report in reports for key, value in report["failures"].items()
    }
    return merged


def rebuild_student(
    connection: psycopg.Connection, student_id: str, index
) -> dict:
    with student_advisory_lock(connection, student_id):
        student = connection.execute(
            """
            SELECT id, status FROM students
            WHERE id = %s AND tenant_id = current_setting('app.tenant_id')
            """,
            (student_id,),
        ).fetchone()
        if student is None:
            raise ValueError(
                f"Student {student_id} does not belong to the current tenant"
            )
        if student["status"] != "active":
            raise ValueError(
                f"Student {student_id} is not active (status={student['status']})"
            )
        deletion = connection.execute(
            """
            SELECT state FROM student_deletions
            WHERE student_id = %s
              AND tenant_id = current_setting('app.tenant_id')
            LIMIT 1
            """,
            (student_id,),
        ).fetchone()
        if deletion is not None:
            raise ValueError(
                f"Student {student_id} has a deletion state ({deletion['state']})"
            )

        with pg.transaction(connection):
            # Phase one removes prior delivery intent and commits only the delete.
            connection.execute(
                """
                DELETE FROM memory_outbox
                WHERE student_id = %s AND tenant_id = current_setting('app.tenant_id')
                """,
                (student_id,),
            )
            repo = OutboxRepository(connection)
            repo.enqueue(
                connection,
                student_id=student_id,
                aggregate_type="student",
                aggregate_id=student_id,
                operation="delete_student",
                payload={"student_id": student_id},
                version=1,
            )
        delete_delivery = _drain_worker(
            connection, student_id, index, max_batches=1
        )
        delete_succeeded = (
            delete_delivery["claimed"] == 1
            and delete_delivery["successful"] == 1
            and delete_delivery["failed"] == 0
            and delete_delivery["deleted"] == 1
        )
        if not delete_succeeded:
            return {
                "student_id": student_id,
                "episodes_enqueued": 0,
                "facts_enqueued": 0,
                "delivery": delete_delivery,
            }

        with pg.transaction(connection):
            repo = OutboxRepository(connection)
            episodes = _enqueue_episodes(connection, student_id, repo)
            facts = _enqueue_facts(connection, student_id, repo)
        upsert_delivery = _drain_worker(connection, student_id, index)
        return {
            "student_id": student_id,
            "episodes_enqueued": episodes,
            "facts_enqueued": facts,
            "delivery": _merge_delivery_reports(delete_delivery, upsert_delivery),
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=None, help="PostgreSQL DSN")
    parser.add_argument("--admin-db", default=None, help="PostgreSQL admin DSN")
    parser.add_argument(
        "--tenant", default=_default_tenant(), help="tenant to rebuild (default tenant_demo)"
    )
    parser.add_argument("--student", default=None, help="rebuild one student only")
    parser.add_argument(
        "--index", default="stub", choices=["stub", "adapter"], help="index backend"
    )
    args = parser.parse_args()

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

        index = InMemoryMnemisIndex() if args.index == "stub" else None
        if index is None:
            from app.memory.mnemis_backend import MnemisMemoryAdapter

            index = MnemisMemoryAdapter()

        connection.execute(
            "SELECT set_config('app.tenant_id', %s, false)", (args.tenant,)
        )
        connection.commit()
        students = (
            [args.student]
            if args.student
            else [
                r["id"]
                for r in connection.execute(
                    """
                    SELECT id FROM students
                    WHERE tenant_id = current_setting('app.tenant_id')
                    """
                ).fetchall()
            ]
        )

        report = {"db": DATABASE_LABEL, "students": []}
        for student_id in students:
            try:
                report["students"].append(
                    rebuild_student(connection, student_id, index)
                )
            except ValueError as exc:
                print(f"Rebuild refused: {exc}", file=sys.stderr)
                return 2
        print(json.dumps(report, indent=2))
        return 2 if any(
            student["delivery"]["failed"] > 0 for student in report["students"]
        ) else 0
    finally:
        pg.quiet_close(connection)
        pg.quiet_close(admin)


if __name__ == "__main__":
    sys.exit(main())
