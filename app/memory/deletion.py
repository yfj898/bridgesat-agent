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

from .outbox import (
    OutboxRepository,
    student_advisory_lock,
    student_advisory_lock_async,
)
from .worker import OutboxWorker

DELETION_STATES = ("requested", "sqlite_deleted", "index_deletion_pending", "verified", "failed")
_ALLOWED_TRANSITIONS = {
    None: {"requested"},
    "requested": {"requested", "sqlite_deleted"},
    "sqlite_deleted": {
        "sqlite_deleted",
        "index_deletion_pending",
        "verified",
        "failed",
    },
    "index_deletion_pending": {
        "index_deletion_pending",
        "verified",
        "failed",
    },
    "verified": {"verified"},
    "failed": {"failed"},
}

# (table, deletion_sql) in dependency order (children first). The SQLite-era
# table names map 1:1 to the PostgreSQL schema (0001-0011).
_PG_DELETIONS: list[tuple[str, str]] = [
    (
        "student_tokens",
        "DELETE FROM student_tokens WHERE student_id = %s "
        "AND tenant_id = current_setting('app.tenant_id', true)",
    ),
    (
        "student_skill_states",
        "DELETE FROM student_skill_states WHERE student_id = %s "
        "AND tenant_id = current_setting('app.tenant_id', true)",
    ),
    (
        "study_plans",
        "DELETE FROM study_plans WHERE student_id = %s "
        "AND tenant_id = current_setting('app.tenant_id', true)",
    ),
    (
        "session_branches",
        "DELETE FROM session_branches "
        "WHERE tenant_id = current_setting('app.tenant_id', true) "
        "AND session_id IN ("
        "SELECT session_id FROM study_sessions "
        "WHERE student_id = %s "
        "AND tenant_id = current_setting('app.tenant_id', true))",
    ),
    (
        "session_items",
        "DELETE FROM session_items "
        "WHERE tenant_id = current_setting('app.tenant_id', true) "
        "AND session_id IN ("
        "SELECT session_id FROM study_sessions "
        "WHERE student_id = %s "
        "AND tenant_id = current_setting('app.tenant_id', true))",
    ),
    (
        "answer_attempts",
        "DELETE FROM answer_attempts WHERE student_id = %s "
        "AND tenant_id = current_setting('app.tenant_id', true)",
    ),
    (
        "study_sessions",
        "DELETE FROM study_sessions WHERE student_id = %s "
        "AND tenant_id = current_setting('app.tenant_id', true)",
    ),
    (
        "learning_events",
        "DELETE FROM learning_events WHERE student_id = %s "
        "AND tenant_id = current_setting('app.tenant_id', true)",
    ),
    (
        "agent_events",
        "DELETE FROM agent_events WHERE student_id = %s "
        "AND tenant_id = current_setting('app.tenant_id', true)",
    ),
    (
        "misconception_evidence",
        "DELETE FROM misconception_evidence WHERE student_id = %s "
        "AND tenant_id = current_setting('app.tenant_id', true)",
    ),
    (
        "learning_episodes",
        "DELETE FROM learning_episodes WHERE student_id = %s "
        "AND tenant_id = current_setting('app.tenant_id', true)",
    ),
    (
        "student_memory_facts",
        "DELETE FROM student_memory_facts WHERE student_id = %s "
        "AND tenant_id = current_setting('app.tenant_id', true)",
    ),
    (
        "intervention_stats",
        "DELETE FROM intervention_stats WHERE student_id = %s "
        "AND tenant_id = current_setting('app.tenant_id', true)",
    ),
    (
        "memory_outbox",
        "DELETE FROM memory_outbox WHERE student_id = %s "
        "AND tenant_id = current_setting('app.tenant_id', true)",
    ),
    (
        "devices",
        "DELETE FROM devices WHERE student_id = %s "
        "AND tenant_id = current_setting('app.tenant_id', true)",
    ),
    (
        "sync_conflicts",
        "DELETE FROM sync_conflicts WHERE student_id = %s "
        "AND tenant_id = current_setting('app.tenant_id', true)",
    ),
    (
        "legacy_mastery_imports",
        "DELETE FROM legacy_mastery_imports WHERE student_id = %s "
        "AND tenant_id = current_setting('app.tenant_id', true)",
    ),
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
        with student_advisory_lock(self.connection, student_id):
            try:
                self._require_active_student(student_id)
                now = utc_now_iso()
                self._set_state(
                    student_id, "requested", started=True, connection=self.connection
                )
                updated = self.connection.execute(
                    """
                    UPDATE students
                    SET status = 'deletion_pending', updated_at = %s
                    WHERE id = %s
                      AND tenant_id = current_setting('app.tenant_id', true)
                      AND status = 'active'
                    RETURNING id
                    """,
                    (now, student_id),
                ).fetchone()
                if updated is None:
                    raise ValueError(
                        f"Student {student_id} is no longer active during deletion request"
                    )
                self.connection.execute(
                    """
                    UPDATE student_tokens
                    SET revoked_at = %s
                    WHERE student_id = %s
                      AND tenant_id = current_setting('app.tenant_id', true)
                      AND revoked_at IS NULL
                    """,
                    (now, student_id),
                )
                self.connection.commit()
            except BaseException:
                try:
                    self.connection.rollback()
                except BaseException:
                    pass
                raise

    def _require_active_student(self, student_id: str) -> None:
        row = self.connection.execute(
            """
            SELECT status
            FROM students
            WHERE id = %s
              AND tenant_id = current_setting('app.tenant_id', true)
            FOR UPDATE
            """,
            (student_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"Student {student_id} does not belong to the current tenant")
        if row["status"] != "active":
            raise ValueError(
                f"Student {student_id} is not active (status={row['status']})"
            )

    def execute_sqlite_deletion(self, student_id: str) -> None:
        """Remove personal rows and enqueue the deletion outbox event in one
        transaction. The students row remains as an auditable tombstone."""
        with student_advisory_lock(self.connection, student_id):
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
                try:
                    self.connection.rollback()
                except BaseException:
                    pass
                raise

    async def complete_index_deletion(self, student_id: str) -> bool:
        """Run the worker, then verify no retrievable memory remains. Returns
        True only after verification; failure stays in a pending/failed state."""
        async with student_advisory_lock_async(self.connection, student_id):
            try:
                self._completion_state(student_id)
                if self.index is None:
                    self._finalize_verified(student_id)
                    return True
                await OutboxWorker(self.connection, index=self.index).run_pending_async(
                    student_id=student_id
                )
                if await self.verify_not_retrievable(student_id):
                    self._finalize_verified(student_id)
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
            except BaseException:
                try:
                    self.connection.rollback()
                except BaseException:
                    pass
                raise

    def _completion_state(self, student_id: str) -> str:
        row = self.connection.execute(
            """
            SELECT state
            FROM student_deletions
            WHERE student_id = %s
              AND tenant_id = current_setting('app.tenant_id', true)
            FOR UPDATE
            """,
            (student_id,),
        ).fetchone()
        state = row["state"] if row is not None else None
        if state not in {"sqlite_deleted", "index_deletion_pending"}:
            raise ValueError(
                "complete_index_deletion requires state sqlite_deleted or "
                f"index_deletion_pending (got {state or 'missing'})"
            )
        return state

    def _finalize_verified(self, student_id: str) -> None:
        try:
            self._set_state(student_id, "verified", connection=self.connection)
            row = self.connection.execute(
                """
                UPDATE students
                SET status = 'deleted', updated_at = %s
                WHERE id = %s
                  AND tenant_id = current_setting('app.tenant_id', true)
                  AND status = 'deletion_pending'
                RETURNING id
                """,
                (utc_now_iso(), student_id),
            ).fetchone()
            if row is None:
                raise ValueError(
                    f"Student {student_id} is not deletion_pending during finalization"
                )
            self.connection.commit()
        except BaseException:
            try:
                self.connection.rollback()
            except BaseException:
                pass
            raise

    async def verify_not_retrievable(self, student_id: str) -> bool:
        """Verify the index holds no retrievable memories for the student.

        Count-based verification keeps working for the deterministic stub.
        An optional adapter is trusted only when it explicitly exposes
        ``verify_student_deleted``; without either capability the answer is
        conservatively False so deletion stays pending instead of claiming
        completion it cannot prove.
        """
        if self.index is None:
            return True
        if hasattr(self.index, "verify_student_deleted"):
            return bool(await self.index.verify_student_deleted(student_id))
        if hasattr(self.index, "count_episodes"):
            episodes = await self.index.count_episodes(student_id)
            facts = await self.index.count_facts(student_id)
            return episodes == 0 and facts == 0
        return False

    def verify_not_retrievable_sync(self, student_id: str) -> bool:
        return asyncio.run(self.verify_not_retrievable(student_id))

    def deletion_status(self, student_id: str) -> str | None:
        row = self.connection.execute(
            """
            SELECT state
            FROM student_deletions
            WHERE student_id = %s
              AND tenant_id = current_setting('app.tenant_id', true)
            """,
            (student_id,),
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
            existing = conn.execute(
                """
                SELECT state
                FROM student_deletions
                WHERE student_id = %s
                  AND tenant_id = current_setting('app.tenant_id', true)
                FOR UPDATE
                """,
                (student_id,),
            ).fetchone()
            current_state = existing["state"] if existing is not None else None
            if state not in _ALLOWED_TRANSITIONS.get(current_state, set()):
                raise ValueError(
                    f"Invalid deletion state transition: "
                    f"{current_state or 'missing'} -> {state}"
                )
            if started:
                import uuid

                if existing is None:
                    conn.execute(
                        """
                        INSERT INTO student_deletions (
                            deletion_id, tenant_id, student_id, state, requested_at
                        ) VALUES (
                            %s, current_setting('app.tenant_id'), %s, %s, %s
                        )
                        """,
                        (f"del_{uuid.uuid4().hex[:12]}", student_id, state, now),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE student_deletions
                        SET state = %s, last_error = NULL
                        WHERE student_id = %s
                          AND tenant_id = current_setting('app.tenant_id', true)
                        """,
                        (state, student_id),
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
                    SET state = %s, {column} = %s, last_error = %s
                    WHERE student_id = %s
                      AND tenant_id = current_setting('app.tenant_id', true)
                    """,
                    (state, now, error, student_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE student_deletions
                    SET state = %s, last_error = %s
                    WHERE student_id = %s
                      AND tenant_id = current_setting('app.tenant_id', true)
                    """,
                    (state, error, student_id),
                )

        if connection is not None:
            apply(connection)
            return
        try:
            apply(self.connection)
            self.connection.commit()
        except BaseException:
            try:
                self.connection.rollback()
            except BaseException:
                pass
            raise

def utc_now_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()
