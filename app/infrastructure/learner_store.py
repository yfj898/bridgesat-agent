from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import psycopg
from psycopg.errors import UniqueViolation

from app.domain.events import LearningEvent
from app.domain.learner import (
    DEFAULT_ALPHA,
    DEFAULT_BETA,
    MisconceptionEvidence,
    MisconceptionState,
    SkillState,
    next_misconception_state,
)
from app.domain.sessions import IllegalTransitionError, SessionState, can_transition

from .pg import transaction

CORE_MISCONCEPTION_SKILLS = {
    "linear_equations",
    "systems_equations",
    "ratios_percentages",
    "functions_models",
}


class DuplicateEventIdError(RuntimeError):
    pass


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


class LearnerStore:
    """Projections derived from immutable events, all in one transaction.

    The rule is: append the event first, then update the projection, inside a
    single transaction. A duplicate event_id is rejected without touching the
    projection.
    """

    def __init__(self, connection: psycopg.Connection) -> None:
        self.connection = connection

    def create_student(self, name: str, daily_minutes: int, target_score: int) -> tuple[str, LearningEvent]:
        student_id = f"stu_{uuid.uuid4().hex[:12]}"
        now = utc_now_iso()
        event = LearningEvent(
            event_id=f"evt_{uuid.uuid4().hex[:16]}",
            student_id=student_id,
            session_id="",
            event_type="STUDENT_CREATED",
            payload={
                "name": name,
                "daily_minutes": daily_minutes,
                "target_score": target_score,
            },
            occurred_at=now,
            received_at=now,
            origin="online",
        ).with_integrity()
        with transaction(self.connection):
            self.connection.execute(
                """
                INSERT INTO students (
                    tenant_id, id, name, daily_minutes, target_score, mastery_json,
                    status, created_at, updated_at
                ) VALUES (
                    current_setting('app.tenant_id'), %s, %s, %s, %s, '{}',
                    'active', %s, %s
                )
                """,
                (student_id, name, daily_minutes, target_score, now, now),
            )
            self._insert_learning_event(self.connection, event)
        return student_id, event

    def create_session(
        self,
        student_id: str,
        session_id: str,
        *,
        device_id: str | None = None,
        origin: str = "online",
        occurred_at: str | None = None,
    ) -> LearningEvent:
        now = occurred_at or utc_now_iso()
        event = LearningEvent(
            event_id=f"evt_{uuid.uuid4().hex[:16]}",
            student_id=student_id,
            session_id=session_id,
            event_type="DIAGNOSTIC_STARTED",
            payload={"session_opened": True},
            occurred_at=now,
            received_at=now,
            device_id=device_id,
            origin=origin,
        ).with_integrity()
        with transaction(self.connection):
            self._insert_learning_event(self.connection, event)
            self.connection.execute(
                """
                INSERT INTO study_sessions (
                    tenant_id, session_id, student_id, session_state, started_at,
                    updated_at
                ) VALUES (
                    current_setting('app.tenant_id'), %s, %s, 'NEW', %s, %s
                )
                """,
                (session_id, student_id, now, now),
            )
        return event

    def get_session_state(self, session_id: str) -> SessionState | None:
        row = self.connection.execute(
            "SELECT session_state, paused_from_state FROM study_sessions WHERE session_id = %s",
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        return SessionState(row["session_state"])

    def transition_session(
        self,
        session_id: str,
        target: SessionState,
        *,
        paused_from: SessionState | None = None,
    ) -> SessionState:
        """Validate and apply a session state transition inside its own
        transaction. Illegal transitions raise IllegalTransitionError and do
        not write the projection."""
        with transaction(self.connection):
            row = self.connection.execute(
                "SELECT session_state FROM study_sessions WHERE session_id = %s FOR UPDATE",
                (session_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown session {session_id}")
            source = SessionState(row["session_state"])
            if not can_transition(source, target):
                raise IllegalTransitionError(source, target)
            self.connection.execute(
                """
                UPDATE study_sessions
                SET session_state = %s, paused_from_state = %s, updated_at = %s
                WHERE session_id = %s
                """,
                (
                    target.value,
                    paused_from.value if paused_from else None,
                    utc_now_iso(),
                    session_id,
                ),
            )
            return target

    def record_answer_evaluation(
        self,
        *,
        student_id: str,
        session_id: str,
        event: LearningEvent,
        content_id: str,
        content_version: int,
        skill: str,
        subskill: str,
        difficulty: int,
        sequence: int,
        selected_choice_id: str,
        correct: bool,
        hint_level: int,
        weight: float,
        validity: str,
        misconception: str | None,
        misconception_source_label: str,
        misconception_confidence_label: str,
        session_state: SessionState,
    ) -> tuple[MisconceptionEvidence | None, SkillState | None]:
        """Append event and update projection atomically."""
        now = event.occurred_at or utc_now_iso()
        evidence: MisconceptionEvidence | None = None
        skill_state: SkillState | None = None
        with transaction(self.connection):
            existing = self.connection.execute(
                "SELECT 1 FROM learning_events WHERE event_id = %s",
                (event.event_id,),
            ).fetchone()
            if existing is not None:
                raise DuplicateEventIdError(event.event_id)

            row = self.connection.execute(
                "SELECT session_state FROM study_sessions WHERE session_id = %s FOR UPDATE",
                (session_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown session {session_id}")
            source_state = SessionState(row["session_state"])
            target_state = SessionState(session_state)
            if not can_transition(source_state, target_state):
                raise IllegalTransitionError(source_state, target_state)

            self._insert_learning_event(self.connection, event)

            self.connection.execute(
                """
                INSERT INTO answer_attempts (
                    tenant_id, attempt_id, event_id, student_id, session_id,
                    content_id, version, sequence, selected_choice_id, correct,
                    hint_level, weight, validity, occurred_at
                ) VALUES (
                    current_setting('app.tenant_id'), %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s
                )
                """,
                (
                    f"att_{uuid.uuid4().hex[:12]}",
                    event.event_id,
                    student_id,
                    session_id,
                    content_id,
                    content_version,
                    sequence,
                    selected_choice_id,
                    1 if correct else 0,
                    hint_level,
                    weight,
                    validity,
                    now,
                ),
            )

            if skill in CORE_MISCONCEPTION_SKILLS:
                self.connection.execute(
                    """
                    INSERT INTO student_skill_states (
                        tenant_id, student_id, skill, alpha, beta, mastery,
                        confidence, evidence_count, correct_streak, incorrect_streak,
                        last_practiced_at, review_due_at, projection_origin, updated_at
                    ) VALUES (
                        current_setting('app.tenant_id'), %s, %s, %s, %s, %s,
                        %s, 0, 0, 0, NULL, NULL, 'live', %s
                    )
                    ON CONFLICT(student_id, skill) DO NOTHING
                    """,
                    (
                        student_id,
                        skill,
                        DEFAULT_ALPHA,
                        DEFAULT_BETA,
                        DEFAULT_ALPHA / (DEFAULT_ALPHA + DEFAULT_BETA),
                        0.0,
                        now,
                    ),
                )
                row = self.connection.execute(
                    """
                    SELECT alpha, beta, evidence_count, correct_streak,
                           incorrect_streak, last_practiced_at, review_due_at
                    FROM student_skill_states
                    WHERE student_id = %s AND skill = %s
                    FOR UPDATE
                    """,
                    (student_id, skill),
                ).fetchone()
                state = SkillState(
                    skill=skill,
                    alpha=row["alpha"],
                    beta=row["beta"],
                    evidence_count=row["evidence_count"],
                    correct_streak=row["correct_streak"],
                    incorrect_streak=row["incorrect_streak"],
                    last_practiced_at=row["last_practiced_at"],
                    review_due_at=row["review_due_at"],
                )
                if validity == "valid":
                    state.record_attempt(correct, weight, now)
                self.connection.execute(
                    """
                    UPDATE student_skill_states
                    SET alpha = %s, beta = %s, mastery = %s, confidence = %s,
                        evidence_count = %s, correct_streak = %s,
                        incorrect_streak = %s, last_practiced_at = %s, updated_at = %s
                    WHERE student_id = %s AND skill = %s
                    """,
                    (
                        state.alpha,
                        state.beta,
                        state.mastery,
                        state.confidence,
                        state.evidence_count,
                        state.correct_streak,
                        state.incorrect_streak,
                        state.last_practiced_at,
                        now,
                        student_id,
                        skill,
                    ),
                )
                skill_state = state

            if misconception is not None:
                # Lock order is session row, core skill row, then misconception key.
                self.connection.execute(
                    """
                    SELECT pg_advisory_xact_lock(
                        hashtextextended(
                            json_build_array(%s::text, %s::text, %s::text)::text,
                            0
                        )
                    )
                    """,
                    (student_id, skill, misconception),
                )
                counts = self.connection.execute(
                    """
                    SELECT COUNT(*) AS total,
                           COUNT(DISTINCT item_id) AS distinct_items
                    FROM misconception_evidence
                    WHERE student_id = %s AND skill = %s AND misconception = %s
                    """,
                    (student_id, skill, misconception),
                ).fetchone()
                state_value = next_misconception_state(
                    int(counts["total"]) + 1, int(counts["distinct_items"])
                )
                evidence_id = f"evid_{uuid.uuid4().hex[:12]}"
                evidence = MisconceptionEvidence(
                    evidence_id=evidence_id,
                    student_id=student_id,
                    session_id=session_id,
                    event_id=event.event_id,
                    skill=skill,
                    subskill=subskill,
                    misconception=misconception,
                    source_label=misconception_source_label,
                    confidence_label=misconception_confidence_label,
                    state=state_value,
                    item_id=content_id,
                    item_version=content_version,
                    observed_at=now,
                )
                self.connection.execute(
                    """
                    INSERT INTO misconception_evidence (
                        tenant_id, evidence_id, student_id, session_id, event_id,
                        skill, subskill, misconception, source_label,
                        confidence_label, state, item_id, item_version, observed_at
                    ) VALUES (
                        current_setting('app.tenant_id'), %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s
                    )
                    """,
                    (
                        evidence.evidence_id,
                        evidence.student_id,
                        evidence.session_id,
                        evidence.event_id,
                        evidence.skill,
                        evidence.subskill,
                        evidence.misconception,
                        evidence.source_label,
                        evidence.confidence_label,
                        evidence.state.value,
                        evidence.item_id,
                        evidence.item_version,
                        evidence.observed_at,
                    ),
                )

            self.connection.execute(
                """
                UPDATE study_sessions
                SET session_state = %s, updated_at = %s
                WHERE session_id = %s
                """,
                (target_state.value, now, session_id),
            )
        return evidence, skill_state

    def _insert_learning_event(
        self, connection: psycopg.Connection, event: LearningEvent
    ) -> None:
        try:
            connection.execute(
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
        except UniqueViolation as exc:
            raise DuplicateEventIdError(event.event_id) from exc

    def get_skill_state(self, student_id: str, skill: str) -> SkillState | None:
        row = self.connection.execute(
            """
            SELECT alpha, beta, evidence_count, correct_streak, incorrect_streak,
                   last_practiced_at, review_due_at, projection_origin
            FROM student_skill_states WHERE student_id = %s AND skill = %s
            """,
            (student_id, skill),
        ).fetchone()
        if row is None:
            return None
        return SkillState(
            skill=skill,
            alpha=row["alpha"],
            beta=row["beta"],
            evidence_count=row["evidence_count"],
            correct_streak=row["correct_streak"],
            incorrect_streak=row["incorrect_streak"],
            last_practiced_at=row["last_practiced_at"],
            review_due_at=row["review_due_at"],
            projection_origin=row["projection_origin"],
        )

    def count_misconception_evidence(
        self, student_id: str, skill: str, misconception: str
    ) -> tuple[int, int]:
        row = self.connection.execute(
            """
            SELECT COUNT(*) AS total, COUNT(DISTINCT item_id) AS distinct_items
            FROM misconception_evidence
            WHERE student_id = %s AND skill = %s AND misconception = %s
            """,
            (student_id, skill, misconception),
        ).fetchone()
        return int(row["total"]), int(row["distinct_items"])
