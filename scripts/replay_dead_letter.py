#!/usr/bin/env python3
"""Replay PostgreSQL dead-letter outbox records after fixing the root cause.

Usage:
    python scripts/replay_dead_letter.py [--db DSN] [--admin-db DSN]
                                         [--tenant TENANT_ID]
                                         [--max-attempts N]

Resets dead_letter rows to pending with a fresh attempt budget, then runs the
worker once per attempt window, honoring the retry schedule.

Delivery uses the real configured index (mirroring app.main): in enhanced
mode that is the Mnemis adapter; otherwise the worker is left index=None and
rows stay pending for the running app to drain. It never marks rows indexed
against a throwaway in-memory stub, which would silently drop indexing.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
import os
import sys
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.infrastructure import pg
from app.infrastructure.migration_runner import migrate_database
from app.memory import MemoryMode, memory_mode
from app.memory.outbox import student_advisory_lock
from app.memory.worker import OutboxWorker


def _default_tenant() -> str:
    return os.getenv("BRIDGESAT_DEFAULT_TENANT", "tenant_demo")


def _make_index():
    if memory_mode() != MemoryMode.ENHANCED:
        return None
    from app.memory.mnemis_backend import MnemisMemoryAdapter

    return MnemisMemoryAdapter()


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer >= 1") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be an integer >= 1")
    return parsed


def _delivery_status_counts(
    connection: psycopg.Connection,
    *,
    outbox_ids: list[str] | None = None,
) -> dict[str, int]:
    if outbox_ids is not None and not outbox_ids:
        return {}
    conditions = ["tenant_id = current_setting('app.tenant_id')"]
    params: list[object] = []
    if outbox_ids is not None:
        conditions.append("outbox_id = ANY(%s)")
        params.append(sorted(outbox_ids))
    rows = connection.execute(
        f"SELECT status, COUNT(*) AS total FROM memory_outbox "
        f"WHERE {' AND '.join(conditions)} GROUP BY status",
        params,
    ).fetchall()
    return {row["status"]: int(row["total"]) for row in rows}


def _next_attempt_at(
    connection: psycopg.Connection,
    *,
    outbox_ids: list[str] | None = None,
) -> str | None:
    if outbox_ids is not None and not outbox_ids:
        return None
    conditions = [
        "tenant_id = current_setting('app.tenant_id')",
        "status IN ('pending', 'retrying', 'deletion_pending', 'processing')",
    ]
    params: list[object] = []
    if outbox_ids is not None:
        conditions.append("outbox_id = ANY(%s)")
        params.append(sorted(outbox_ids))
    rows = connection.execute(
        f"""
        SELECT next_attempt_at
        FROM memory_outbox
        WHERE {' AND '.join(conditions)}
        """,
        params,
    ).fetchall()
    values = [row["next_attempt_at"] for row in rows if row["next_attempt_at"]]
    return (
        min(values, key=lambda value: datetime.fromisoformat(value).astimezone(UTC))
        if values
        else None
    )


def replay(
    connection: psycopg.Connection,
    index,
    max_attempts: int,
    *,
    now: str | None = None,
) -> dict:
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")
    virtual_now = datetime.fromisoformat(now or now_iso()).astimezone(UTC).isoformat()
    candidate_rows = connection.execute(
        """
        SELECT outbox_id, student_id
        FROM memory_outbox AS outbox
        WHERE outbox.status = 'dead_letter'
          AND outbox.tenant_id = current_setting('app.tenant_id')
          AND NOT EXISTS (
              SELECT 1 FROM student_deletions AS sd
              WHERE sd.student_id = outbox.student_id
                AND sd.tenant_id = outbox.tenant_id
                AND sd.state IN ('requested', 'sqlite_deleted', 'index_deletion_pending')
          )
          AND NOT EXISTS (
              SELECT 1 FROM students AS s
              WHERE s.id = outbox.student_id
                AND s.status <> 'active'
          )
        ORDER BY outbox.student_id, outbox.outbox_id
        """
    ).fetchall()
    reset_ids: list[str] = []
    for student_id in sorted({row["student_id"] for row in candidate_rows}):
        with student_advisory_lock(connection, student_id):
            rows = connection.execute(
                """
                SELECT outbox_id
                FROM memory_outbox
                WHERE status = 'dead_letter'
                  AND tenant_id = current_setting('app.tenant_id')
                  AND student_id = %s
                ORDER BY outbox_id
                FOR UPDATE
                """,
                (student_id,),
            ).fetchall()
            for row in rows:
                reset = connection.execute(
                    """
                    UPDATE memory_outbox
                    SET status = 'pending', attempt_count = 0,
                        next_attempt_at = %s, last_error = NULL
                    WHERE outbox_id = %s
                      AND tenant_id = current_setting('app.tenant_id')
                      AND student_id = %s
                      AND status = 'dead_letter'
                    RETURNING outbox_id
                    """,
                    (virtual_now, row["outbox_id"], student_id),
                ).fetchone()
                if reset is not None:
                    reset_ids.append(reset["outbox_id"])
            connection.commit()
    processed = 0
    successful = 0
    failed = 0
    failures: dict[str, str] = {}
    if index is not None:
        worker = OutboxWorker(
            connection,
            index=index,
            batch_size=max(1, len(reset_ids)),
        )
        for _ in range(max_attempts):
            next_attempt = _next_attempt_at(connection, outbox_ids=reset_ids)
            if next_attempt is None:
                break
            next_datetime = datetime.fromisoformat(next_attempt).astimezone(UTC)
            if next_datetime > datetime.fromisoformat(virtual_now):
                virtual_now = next_datetime.isoformat()
            processed += worker.run_pending(
                now=virtual_now, outbox_ids=reset_ids
            )
            successful += worker.successful_total
            failed += worker.failed_total
            failures.update(worker.last_errors)
    else:
        print(
            "Local memory mode: rows reset to pending; the app's own worker "
            "will deliver them on next startup.",
            file=sys.stderr,
        )
    statuses = _delivery_status_counts(
        connection,
        outbox_ids=reset_ids if reset_ids else None,
    )
    report = {
        "reset_rows": len(reset_ids),
        "processed": processed,
        "successful": successful,
        "failed": failed,
        "failures": failures,
        "pending": statuses.get("pending", 0),
        "retrying": statuses.get("retrying", 0),
        "processing": statuses.get("processing", 0),
        "dead_letter": statuses.get("dead_letter", 0),
        "indexed": statuses.get("indexed", 0),
        "deleted": statuses.get("deleted", 0),
        "mode": "enhanced" if index is not None else "local",
    }
    if reset_ids:
        report["deletion_pending"] = statuses.get("deletion_pending", 0)
    return report


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=None, help="PostgreSQL DSN")
    parser.add_argument("--admin-db", default=None, help="PostgreSQL admin DSN")
    parser.add_argument(
        "--tenant", default=_default_tenant(), help="tenant to replay (default tenant_demo)"
    )
    parser.add_argument("--max-attempts", type=_positive_int, default=6)
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
        connection.execute(
            "SELECT set_config('app.tenant_id', %s, false)", (args.tenant,)
        )
        connection.commit()
        report = replay(connection, _make_index(), args.max_attempts)
        print(json.dumps(report, indent=2))
        remaining = sum(
            report.get(status, 0)
            for status in (
                "pending",
                "retrying",
                "processing",
                "deletion_pending",
                "dead_letter",
            )
        )
        return 2 if report["reset_rows"] and remaining else 0
    finally:
        pg.quiet_close(connection)
        pg.quiet_close(admin)


if __name__ == "__main__":
    sys.exit(main())
