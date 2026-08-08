from __future__ import annotations

import json
from collections.abc import Callable

import psycopg
from psycopg.errors import UniqueViolation

from app.domain.events import AgentEvent, LearningEvent


class DuplicateEventError(RuntimeError):
    pass


class EventStore:
    def __init__(self, connection: psycopg.Connection) -> None:
        self.connection = connection

    def append_learning_event(
        self,
        event: LearningEvent,
        *,
        on_duplicate: str = "ignore",
    ) -> bool:
        """Insert an immutable learning event for the current tenant."""
        try:
            self.connection.execute(
                """
                INSERT INTO learning_events (
                    tenant_id, event_id, student_id, session_id, event_type,
                    payload_json, policy_version, content_version, occurred_at,
                    received_at, device_id, device_sequence, origin, integrity_hash
                ) VALUES (
                    current_setting('app.tenant_id'), %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s
                )
                """,
                (
                    event.event_id,
                    event.student_id,
                    event.session_id,
                    event.event_type.value,
                    json.dumps(event.payload, sort_keys=True),
                    event.policy_version,
                    event.content_version,
                    event.occurred_at,
                    event.received_at,
                    event.device_id,
                    event.device_sequence,
                    event.origin,
                    event.integrity_hash,
                ),
            )
            self.connection.commit()
            return True
        except UniqueViolation as exc:
            self.connection.rollback()
            if on_duplicate == "raise":
                raise DuplicateEventError(f"Duplicate event {event.event_id}") from exc
            return False

    def append_agent_event(self, event: AgentEvent, *, on_duplicate: str = "ignore") -> bool:
        try:
            self.connection.execute(
                """
                INSERT INTO agent_events (
                    tenant_id, event_id, student_id, session_id, source_event_id,
                    state_before, state_after, action, action_payload_json,
                    reason_code, reason_text, policy_version, taxonomy_version,
                    content_version, referenced_content_json, episode_ids_json,
                    source, created_at
                ) VALUES (
                    current_setting('app.tenant_id'), %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                """,
                (
                    event.event_id,
                    event.student_id,
                    event.session_id,
                    event.source_event_id,
                    event.state_before,
                    event.state_after,
                    event.action,
                    json.dumps(event.action_payload, sort_keys=True),
                    event.reason_code,
                    event.reason_text,
                    event.policy_version,
                    event.taxonomy_version,
                    event.content_version,
                    json.dumps(event.referenced_content),
                    json.dumps(event.episode_ids),
                    event.source,
                    event.created_at,
                ),
            )
            self.connection.commit()
            return True
        except UniqueViolation as exc:
            self.connection.rollback()
            if on_duplicate == "raise":
                raise DuplicateEventError(f"Duplicate agent event {event.event_id}") from exc
            return False

    def learning_event_exists(self, event_id: str) -> bool:
        row = self.connection.execute(
            "SELECT 1 AS hit FROM learning_events WHERE event_id = %s",
            (event_id,),
        ).fetchone()
        return row is not None

    def get_learning_events(
        self,
        student_id: str | None = None,
        session_id: str | None = None,
        event_type: str | None = None,
    ) -> list[LearningEvent]:
        clauses: list[str] = []
        params: list[object] = []
        if student_id is not None:
            clauses.append("student_id = %s")
            params.append(student_id)
        if session_id is not None:
            clauses.append("session_id = %s")
            params.append(session_id)
        if event_type is not None:
            clauses.append("event_type = %s")
            params.append(event_type)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.connection.execute(
            f"SELECT * FROM learning_events {where} ORDER BY received_at",
            params,
        ).fetchall()
        return [_row_to_learning_event(row) for row in rows]

    def get_agent_events(
        self,
        student_id: str | None = None,
        session_id: str | None = None,
    ) -> list[AgentEvent]:
        clauses: list[str] = []
        params: list[object] = []
        if student_id is not None:
            clauses.append("student_id = %s")
            params.append(student_id)
        if session_id is not None:
            clauses.append("session_id = %s")
            params.append(session_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.connection.execute(
            f"SELECT * FROM agent_events {where} ORDER BY created_at",
            params,
        ).fetchall()
        return [_row_to_agent_event(row) for row in rows]


def run_in_transaction(
    connection: psycopg.Connection,
    fn: Callable[[psycopg.Connection], object],
) -> None:
    try:
        fn(connection)
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


def _row_to_learning_event(row: dict) -> LearningEvent:
    return LearningEvent(
        event_id=row["event_id"],
        student_id=row["student_id"],
        session_id=row["session_id"],
        event_type=row["event_type"],
        payload=json.loads(row["payload_json"]),
        policy_version=row["policy_version"],
        content_version=row["content_version"],
        occurred_at=row["occurred_at"],
        received_at=row["received_at"],
        device_id=row["device_id"],
        device_sequence=row["device_sequence"],
        origin=row["origin"],
        integrity_hash=row["integrity_hash"],
    )


def _row_to_agent_event(row: dict) -> AgentEvent:
    return AgentEvent(
        event_id=row["event_id"],
        student_id=row["student_id"],
        session_id=row["session_id"],
        source_event_id=row["source_event_id"],
        state_before=row["state_before"],
        state_after=row["state_after"],
        action=row["action"],
        action_payload=json.loads(row["action_payload_json"] or "{}"),
        reason_code=row["reason_code"],
        reason_text=row["reason_text"],
        policy_version=row["policy_version"],
        taxonomy_version=row["taxonomy_version"],
        content_version=row["content_version"],
        referenced_content=json.loads(row["referenced_content_json"] or "[]"),
        episode_ids=json.loads(row["episode_ids_json"] or "[]"),
        source=row["source"],
        created_at=row["created_at"],
    )
