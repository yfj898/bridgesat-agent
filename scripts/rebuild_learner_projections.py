#!/usr/bin/env python3
"""Rebuild learner projections from the immutable event log.

Recovery capability required by API_AND_OPERATIONS section 7:

    rebuild learner projections from events

For each student (or `--student` only) this:
1. deletes the derived projection rows (study_sessions, answer_attempts,
   student_skill_states, misconception_evidence, sync_conflicts);
2. replays `learning_events` in occurred_at/received_at order through the
   same SyncService apply path (`insert_event_row=False`, so the immutable
   log is never re-written);
3. reports row counts before and after.

Usage:
    python scripts/rebuild_learner_projections.py [--db content/registry.db]
                                                  [--student STUDENT_ID]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.infrastructure.database import connect, transaction
from app.infrastructure.migration_runner import apply_migrations
from app.sync.protocol import SyncConflict, SyncErrorCode, SyncRejectedEvent
from app.sync.service import SyncService

DEFAULT_DB = ROOT / "content" / "registry.db"

PROJECTION_TABLES = (
    "study_sessions",
    "answer_attempts",
    "student_skill_states",
    "misconception_evidence",
    "sync_conflicts",
)

REPLAYABLE_EVENT_TYPES = {
    "ANSWER_SUBMITTED",
    "SESSION_COMPLETED",
    "DIAGNOSTIC_STARTED",
    "HINT_REQUESTED",
    "CONTENT_PRESENTED",
    "WORKED_EXAMPLE_PRESENTED",
    "MICRO_LESSON_PRESENTED",
    "DIAGNOSTIC_COMPLETED",
    "PLAN_READY",
}


def _student_ids(db: Path, student_id: str | None) -> list[str]:
    if student_id:
        return [student_id]
    with connect(db) as connection:
        rows = connection.execute(
            "SELECT DISTINCT student_id FROM learning_events ORDER BY student_id"
        ).fetchall()
        return [row["student_id"] for row in rows]


def _counts(db: Path, student_id: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    with connect(db) as connection:
        for table in PROJECTION_TABLES:
            counts[table] = connection.execute(
                f"SELECT COUNT(*) AS total FROM {table} WHERE student_id = ?",
                (student_id,),
            ).fetchone()["total"]
    return counts


def _clear_projections(db: Path, student_id: str) -> None:
    # Recovery operation: projection rows reference each other, so foreign
    # key checks are disabled for the explicit rebuild (the immutable
    # learning_events log is never touched).
    with connect(db) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        with transaction(connection):
            for table in PROJECTION_TABLES:
                connection.execute(
                    f"DELETE FROM {table} WHERE student_id = ?", (student_id,)
                )


def _events_for(db: Path, student_id: str) -> list[dict]:
    with connect(db) as connection:
        return [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM learning_events WHERE student_id = ? "
                "ORDER BY occurred_at, received_at, rowid",
                (student_id,),
            ).fetchall()
        ]


def _envelope_from_row(row: dict):
    from app.sync.protocol import SyncEventEnvelope

    return SyncEventEnvelope(
        event_id=row["event_id"],
        student_id=row["student_id"],
        session_id=row["session_id"],
        session_branch_id="branch_rebuild",
        device_id=row["device_id"],
        device_sequence=row["device_sequence"],
        event_type=row["event_type"],
        payload=json.loads(row["payload_json"]),
        content_pack_version=row["content_version"],
        question_id=None,
        question_version=None,
        policy_version=row["policy_version"],
        depends_on_event_ids=[],
        device_occurred_at=row["occurred_at"],
        integrity_hash=row["integrity_hash"],
    )


def rebuild_student(db: Path, student_id: str, sync: SyncService) -> dict:
    before = _counts(db, student_id)
    events = _events_for(db, student_id)
    replayable = [row for row in events if row["event_type"] in REPLAYABLE_EVENT_TYPES]
    skipped = len(events) - len(replayable)
    _clear_projections(db, student_id)

    accepted: list[str] = []
    rejected: list[SyncRejectedEvent] = []
    conflicts: list[SyncConflict] = []
    server_events: list[dict] = []
    for row in replayable:
        sync._apply_event(  # noqa: SLF001 - same apply path as sync, replay-only
            _envelope_from_row(row),
            accepted,
            rejected,
            conflicts,
            server_events,
            insert_event_row=False,
        )

    after = _counts(db, student_id)
    return {
        "student_id": student_id,
        "events_replayed": len(replayable),
        "skipped_server_events": skipped,
        "accepted": len(accepted),
        "rejected": [r.code for r in rejected],
        "projection_rows_before": before,
        "projection_rows_after": after,
        "row_delta": {t: after[t] - before[t] for t in PROJECTION_TABLES},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--student", default=None, help="rebuild one student only")
    args = parser.parse_args()

    if not args.db.is_file():
        print(f"Database {args.db} not found", file=sys.stderr)
        return 1
    apply_migrations(args.db)
    sync = SyncService(args.db)

    reports = [rebuild_student(args.db, sid, sync) for sid in _student_ids(args.db, args.student)]
    failed = [r for r in reports if r["rejected"] or r["events_replayed"] != r["accepted"]]
    for report in reports:
        status = "OK" if report not in failed else "FAIL"
        print(f"{status} {report['student_id']}: {report['events_replayed']} events "
              f"({report['skipped_server_events']} server events skipped), "
              f"projection rows {sum(report['projection_rows_after'].values())}")
    if failed:
        print(json.dumps(failed, indent=2), file=sys.stderr)
        return 2
    print(f"Rebuilt projections for {len(reports)} student(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
