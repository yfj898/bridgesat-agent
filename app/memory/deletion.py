"""Student memory deletion protocol (MEMORY_CONSISTENCY §11).

A distributed process: mark the learner deletion_pending, stop new writes,
remove or tombstone SQLite personal records together with a delete_student
outbox event (one transaction), have the worker delete the Mnemis data, then
verify no retrievable memory remains before reporting completion. States:
requested -> sqlite_deleted -> index_deletion_pending -> verified | failed.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from app.infrastructure.database import connect, transaction

from .outbox import OutboxRepository
from .worker import OutboxWorker

DELETION_STATES = ("requested", "sqlite_deleted", "index_deletion_pending", "verified", "failed")

# (table, deletion_sql) in dependency order (children first).
_SQLITE_DELETIONS: list[tuple[str, str]] = [
    ("student_tokens", "DELETE FROM student_tokens WHERE student_id = ?"),
    ("student_skill_states", "DELETE FROM student_skill_states WHERE student_id = ?"),
    ("study_plans", "DELETE FROM study_plans WHERE student_id = ?"),
    (
        "session_branches",
        "DELETE FROM session_branches WHERE session_id IN (SELECT session_id FROM study_sessions WHERE student_id = ?)",
    ),
    (
        "session_items",
        "DELETE FROM session_items WHERE session_id IN (SELECT session_id FROM study_sessions WHERE student_id = ?)",
    ),
    ("answer_attempts", "DELETE FROM answer_attempts WHERE student_id = ?"),
    ("study_sessions", "DELETE FROM study_sessions WHERE student_id = ?"),
    ("learning_events", "DELETE FROM learning_events WHERE student_id = ?"),
    ("agent_events", "DELETE FROM agent_events WHERE student_id = ?"),
    ("misconception_evidence", "DELETE FROM misconception_evidence WHERE student_id = ?"),
    ("learning_episodes", "DELETE FROM learning_episodes WHERE student_id = ?"),
    ("student_memory_facts", "DELETE FROM student_memory_facts WHERE student_id = ?"),
    ("intervention_stats", "DELETE FROM intervention_stats WHERE student_id = ?"),
    ("memory_outbox", "DELETE FROM memory_outbox WHERE student_id = ?"),
    ("devices", "DELETE FROM devices WHERE student_id = ?"),
    ("sync_conflicts", "DELETE FROM sync_conflicts WHERE student_id = ?"),
    ("legacy_mastery_imports", "DELETE FROM legacy_mastery_imports WHERE student_id = ?"),
]


class StudentMemoryDeletionService:
    def __init__(
        self,
        database_path: Path,
        index: Any | None = None,
        *,
        outbox: OutboxRepository | None = None,
    ) -> None:
        self.database_path = database_path
        self.outbox = outbox or OutboxRepository(database_path)
        self.index = index

    def request_deletion(self, student_id: str) -> None:
        with connect(self.database_path) as connection:
            with transaction(connection):
                self._set_state(student_id, "requested", connection=connection, started=True)
                connection.execute(
                    "UPDATE students SET status = 'deletion_pending', updated_at = ? WHERE id = ?",
                    (utc_now_iso(), student_id),
                )

    def execute_sqlite_deletion(self, student_id: str) -> None:
        """Remove personal rows and enqueue the deletion outbox event in one
        transaction. The students row remains as an auditable tombstone."""
        with connect(self.database_path) as connection:
            with transaction(connection):
                for _, statement in _SQLITE_DELETIONS:
                    connection.execute(statement, (student_id,))
                self.outbox.enqueue(
                    connection,
                    student_id=student_id,
                    aggregate_type="student",
                    aggregate_id=student_id,
                    operation="delete_student",
                    payload={"student_id": student_id},
                    version=1,
                )
                self._set_state(student_id, "sqlite_deleted", connection=connection)

    async def complete_index_deletion(self, student_id: str) -> bool:
        """Run the worker, then verify no retrievable memory remains. Returns
        True only after verification; failure stays in a pending/failed state."""
        if self.index is None:
            self._set_state(student_id, "verified")
            self._mark_student_deleted(student_id)
            return True
        await OutboxWorker(self.database_path, index=self.index).run_pending_async()
        if await self.verify_not_retrievable(student_id):
            self._set_state(student_id, "verified")
            self._mark_student_deleted(student_id)
            return True
        outbox_rows = [
            r
            for r in self.outbox.list_by_status("dead_letter")
            if r.operation == "delete_student" and r.aggregate_id == student_id
        ]
        if outbox_rows:
            self._set_state(student_id, "failed", error=outbox_rows[0].last_error)
        else:
            self._set_state(student_id, "index_deletion_pending")
        return False

    async def verify_not_retrievable(self, student_id: str) -> bool:
        """Verify the index holds no retrievable memories for the student."""
        if self.index is None:
            return True
        if hasattr(self.index, "count_episodes"):
            episodes = await self.index.count_episodes(student_id)
            facts = await self.index.count_facts(student_id)
            return episodes == 0 and facts == 0
        return False

    def verify_not_retrievable_sync(self, student_id: str) -> bool:
        return asyncio.run(self.verify_not_retrievable(student_id))

    def deletion_status(self, student_id: str) -> str | None:
        with connect(self.database_path) as connection:
            row = connection.execute(
                "SELECT state FROM student_deletions WHERE student_id = ?", (student_id,)
            ).fetchone()
        return row["state"] if row else None

    def _set_state(
        self,
        student_id: str,
        state: str,
        *,
        connection: Any | None = None,
        started: bool = False,
        error: str | None = None,
    ) -> None:
        now = utc_now_iso()

        def apply(conn: Any) -> None:
            if started:
                conn.execute(
                    """
                    INSERT INTO student_deletions (student_id, state, requested_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(student_id) DO UPDATE SET state = excluded.state
                    """,
                    (student_id, state, now),
                )
                return
            column = {
                "sqlite_deleted": "sqlite_deleted_at",
                "index_deletion_pending": "index_deletion_pending_at",
                "verified": "verified_at",
            }.get(state)
            if column:
                conn.execute(
                    f"""
                    UPDATE student_deletions
                    SET state = ?, {column} = ?, last_error = ?
                    WHERE student_id = ?
                    """,
                    (state, now, error, student_id),
                )
            else:
                conn.execute(
                    "UPDATE student_deletions SET state = ?, last_error = ? WHERE student_id = ?",
                    (state, error, student_id),
                )

        if connection is not None:
            apply(connection)
            return
        with connect(self.database_path) as connection:
            apply(connection)

    def _mark_student_deleted(self, student_id: str) -> None:
        with connect(self.database_path) as connection:
            connection.execute(
                "UPDATE students SET status = 'deleted', updated_at = ? WHERE id = ?",
                (utc_now_iso(), student_id),
            )

def utc_now_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()
