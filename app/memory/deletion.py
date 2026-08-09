"""Student memory deletion protocol (MEMORY_CONSISTENCY §11).

A distributed process: mark the learner deletion_pending, stop new writes,
remove or tombstone personal records together with a delete_student outbox
event (one transaction), have the worker delete the Mnemis data, then verify
no retrievable memory remains before reporting completion. States:
requested -> sqlite_deleted -> index_deletion_pending -> verified | failed.
"""

from __future__ import annotations

import asyncio
from typing import Any

import psycopg

from .outbox import OutboxRepository
from .worker import OutboxWorker

DELETION_STATES = ("requested", "sqlite_deleted", "index_deletion_pending", "verified", "failed")

# (table, deletion_sql) in dependency order (children first). The SQLite-era
# table names map 1:1 to the PostgreSQL schema (0001-0011).
_PG_DELETIONS: list[tuple[str, str]] = [
    ("student_tokens", "DELETE FROM student_tokens WHERE student_id = %s"),
    ("student_skill_states", "DELETE FROM student_skill_states WHERE student_id = %s"),
    ("study_plans", "DELETE FROM study_plans WHERE student_id = %s"),
    (
        "session_branches",
        "DELETE FROM session_branches WHERE session_id IN (SELECT session_id FROM study_sessions WHERE student_id = %s)",
    ),
    (
        "session_items",
        "DELETE FROM session_items WHERE session_id IN (SELECT session_id FROM study_sessions WHERE student_id = %s)",
    ),
    ("answer_attempts", "DELETE FROM answer_attempts WHERE student_id = %s"),
    ("study_sessions", "DELETE FROM study_sessions WHERE student_id = %s"),
    ("learning_events", "DELETE FROM learning_events WHERE student_id = %s"),
    ("agent_events", "DELETE FROM agent_events WHERE student_id = %s"),
    ("misconception_evidence", "DELETE FROM misconception_evidence WHERE student_id = %s"),
    ("learning_episodes", "DELETE FROM learning_episodes WHERE student_id = %s"),
    ("student_memory_facts", "DELETE FROM student_memory_facts WHERE student_id = %s"),
    ("intervention_stats", "DELETE FROM intervention_stats WHERE student_id = %s"),
    ("memory_outbox", "DELETE FROM memory_outbox WHERE student_id = %s"),
    ("devices", "DELETE FROM devices WHERE student_id = %s"),
    ("sync_conflicts", "DELETE FROM sync_conflicts WHERE student_id = %s"),
    ("legacy_mastery_imports", "DELETE FROM legacy_mastery_imports WHERE student_id = %s"),
]


class StudentMemoryDeletionService:
    def __init__(
        self,
        connection: psycopg.Connection,
        index: Any | None = None,
        *,
        outbox: OutboxRepository | None = None,
    ) -> None:
        self.connection = connection
        self.outbox = outbox or OutboxRepository(connection)
        self.index = index

    def request_deletion(self, student_id: str) -> None:
        try:
            self._set_state(student_id, "requested", started=True, connection=self.connection)
            self.connection.execute(
                "UPDATE students SET status = 'deletion_pending', updated_at = %s WHERE id = %s",
                (utc_now_iso(), student_id),
            )
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise

    def execute_sqlite_deletion(self, student_id: str) -> None:
        """Remove personal rows and enqueue the deletion outbox event in one
        transaction. The students row remains as an auditable tombstone."""
        try:
            for _, statement in _PG_DELETIONS:
                self.connection.execute(statement, (student_id,))
            self.outbox.enqueue(
                self.connection,
                student_id=student_id,
                aggregate_type="student",
                aggregate_id=student_id,
                operation="delete_student",
                payload={"student_id": student_id},
                version=1,
            )
            self._set_state(student_id, "sqlite_deleted", connection=self.connection)
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise

    async def complete_index_deletion(self, student_id: str) -> bool:
        """Run the worker, then verify no retrievable memory remains. Returns
        True only after verification; failure stays in a pending/failed state."""
        if self.index is None:
            self._set_state(student_id, "verified")
            self._mark_student_deleted(student_id)
            return True
        await OutboxWorker(self.connection, index=self.index).run_pending_async()
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
        row = self.connection.execute(
            "SELECT state FROM student_deletions WHERE student_id = %s", (student_id,)
        ).fetchone()
        return row["state"] if row else None

    def _set_state(
        self,
        student_id: str,
        state: str,
        *,
        connection: psycopg.Connection | None = None,
        started: bool = False,
        error: str | None = None,
    ) -> None:
        now = utc_now_iso()

        def apply(conn: psycopg.Connection) -> None:
            if started:
                import uuid

                conn.execute(
                    """
                    INSERT INTO student_deletions (
                        deletion_id, tenant_id, student_id, state, requested_at
                    ) VALUES (
                        %s, current_setting('app.tenant_id'), %s, %s, %s
                    )
                    ON CONFLICT(student_id) DO UPDATE SET state = excluded.state
                    """,
                    (f"del_{uuid.uuid4().hex[:12]}", student_id, state, now),
                )
                conn.commit()
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
                    SET state = %s, {column} = %s, last_error = %s
                    WHERE student_id = %s
                    """,
                    (state, now, error, student_id),
                )
            else:
                conn.execute(
                    "UPDATE student_deletions SET state = %s, last_error = %s WHERE student_id = %s",
                    (state, error, student_id),
                )

        if connection is not None:
            apply(connection)
            return
        try:
            apply(self.connection)
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise

    def _mark_student_deleted(self, student_id: str) -> None:
        self.connection.execute(
            "UPDATE students SET status = 'deleted', updated_at = %s WHERE id = %s",
            (utc_now_iso(), student_id),
        )
        self.connection.commit()


def utc_now_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()