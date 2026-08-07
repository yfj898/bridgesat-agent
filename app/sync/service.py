"""Sync service: device registration, event batch processing, snapshots.

Implements the server side of SYNC_PROTOCOL.md:

1. authenticate student and device;
2. validate batch size and schemas;
3. verify integrity hashes;
4. identify duplicate event IDs;
5. validate referenced content versions;
6. store valid events append-only;
7. apply events to projections in server receive order with domain rules;
8. detect semantic conflicts;
9. generate server-side Agent events where applicable;
10. commit transaction;
11. return acknowledgements and updated snapshot metadata.

Server never trusts client-computed mastery values: offline ANSWER_SUBMITTED
events are re-scored against the exact referenced question version.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

from app.domain.learner import SkillState
from app.domain.sessions import SessionState
from app.infrastructure.database import connect, transaction
from app.infrastructure.event_store import EventStore
from app.infrastructure.learner_store import LearnerStore
from app.infrastructure.migration_runner import apply_migrations
from app.memory.sqlite_backend import SQLiteMemory

from .protocol import (
    OFFLINE_POLICY_VERSION,
    ConflictType,
    DeviceRegistration,
    SyncConflict,
    SyncErrorCode,
    SyncEventEnvelope,
    SyncRejectedEvent,
    SyncRequest,
    SyncResponse,
    SnapshotResponse,
)
from .versioned_scoring import VersionedAnswerKey


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


class DeviceNotFoundError(RuntimeError):
    pass


class DeviceRevokedError(RuntimeError):
    pass


class SyncService:
    def __init__(self, database_path: Path) -> None:
        self.db = database_path
        apply_migrations(database_path)
        self.events = EventStore(database_path)
        self.learner = LearnerStore(database_path)
        self.memory = SQLiteMemory(database_path)
        self.answer_keys = VersionedAnswerKey()

    # ------------------------------------------------------------------
    # Devices
    # ------------------------------------------------------------------

    def register_device(
        self,
        student_id: str,
        device_name: str | None,
        device_id: str | None = None,
    ) -> DeviceRegistration:
        if not self._student_exists(student_id):
            raise KeyError(f"Unknown student {student_id}")
        device_id = device_id or f"dev_{uuid.uuid4().hex[:12]}"
        now = _utc_now_iso()
        with connect(self.db) as connection:
            connection.execute(
                """
                INSERT INTO devices (device_id, student_id, device_name, status, created_at)
                VALUES (?, ?, ?, 'active', ?)
                """,
                (device_id, student_id, device_name, now),
            )
        return DeviceRegistration(device_id=device_id, student_id=student_id, status="active")

    def revoke_device(self, device_id: str, student_id: str) -> None:
        now = _utc_now_iso()
        with connect(self.db) as connection:
            cursor = connection.execute(
                "UPDATE devices SET status = 'revoked', revoked_at = ? "
                "WHERE device_id = ? AND student_id = ?",
                (now, device_id, student_id),
            )
            if cursor.rowcount == 0:
                raise DeviceNotFoundError(f"Device {device_id} not found")

    def _student_exists(self, student_id: str) -> bool:
        with connect(self.db) as connection:
            row = connection.execute(
                "SELECT 1 FROM students WHERE id = ?", (student_id,)
            ).fetchone()
            return row is not None

    def _verify_device(self, device_id: str, student_id: str) -> None:
        with connect(self.db) as connection:
            row = connection.execute(
                "SELECT status FROM devices WHERE device_id = ? AND student_id = ?",
                (device_id, student_id),
            ).fetchone()
        if row is None:
            raise DeviceNotFoundError(f"Device {device_id} not registered")
        if row["status"] != "active":
            raise DeviceRevokedError(f"Device {device_id} revoked")

    # ------------------------------------------------------------------
    # Batch processing
    # ------------------------------------------------------------------

    def process_batch(self, request: SyncRequest) -> SyncResponse:
        if len(request.events) > 100:
            return SyncResponse(
                new_snapshot_version=0,
                new_server_cursor="",
                rejected_events=[
                    SyncRejectedEvent(
                        event_id="",
                        code=SyncErrorCode.PAYLOAD_TOO_LARGE.value,
                        retryable=False,
                    )
                ],
            )
        self._verify_device(request.device_id, request.student_id)
        if not self._student_exists(request.student_id):
            return SyncResponse(
                new_snapshot_version=0,
                new_server_cursor="",
                rejected_events=[
                    SyncRejectedEvent(
                        event_id="",
                        code=SyncErrorCode.UNAUTHORIZED_STUDENT.value,
                        retryable=False,
                    )
                ],
            )

        accepted: list[str] = []
        duplicates: list[str] = []
        rejected: list[SyncRejectedEvent] = []
        conflicts: list[SyncConflict] = []
        server_agent_events: list[dict] = []

        for envelope in request.events:
            if not self._verify_integrity(envelope):
                rejected.append(
                    SyncRejectedEvent(
                        event_id=envelope.event_id,
                        code=SyncErrorCode.INVALID_SCHEMA.value,
                        retryable=False,
                    )
                )
                continue
            if self.events.learning_event_exists(envelope.event_id):
                duplicates.append(envelope.event_id)
                continue

            dependency_error = self._missing_dependency(request.student_id, envelope)
            if dependency_error is not None:
                rejected.append(dependency_error)
                continue

            try:
                self._apply_event(envelope, accepted, rejected, conflicts, server_agent_events)
            except (DeviceNotFoundError, DeviceRevokedError):
                raise
            except Exception:
                rejected.append(
                    SyncRejectedEvent(
                        event_id=envelope.event_id,
                        code=SyncErrorCode.INTERNAL_RETRYABLE.value,
                        retryable=True,
                    )
                )

        snapshot = self.build_snapshot(request.student_id)
        return SyncResponse(
            accepted_event_ids=accepted,
            duplicate_event_ids=duplicates,
            rejected_events=rejected,
            conflicts=conflicts,
            new_snapshot_version=snapshot.snapshot_version,
            new_server_cursor=snapshot.server_cursor,
            server_events=server_agent_events,
            required_content_packs=self.answer_keys.list_versions(),
            memory_snapshot=snapshot.strategy_memory,
            sync_status="complete",
        )

    def _verify_integrity(self, envelope: SyncEventEnvelope) -> bool:
        if envelope.integrity_hash is None:
            return True
        digest = hashlib.sha256()
        digest.update(envelope.event_type.encode("utf-8"))
        digest.update(b"\x00")
        canonical = json.dumps(envelope.payload, sort_keys=True, separators=(",", ":"))
        digest.update(canonical.encode("utf-8"))
        return envelope.integrity_hash == f"sha256:{digest.hexdigest()}"

    def _missing_dependency(
        self, student_id: str, envelope: SyncEventEnvelope
    ) -> SyncRejectedEvent | None:
        for dep in envelope.depends_on_event_ids:
            if not self.events.learning_event_exists(dep):
                return SyncRejectedEvent(
                    event_id=envelope.event_id,
                    code=SyncErrorCode.MISSING_DEPENDENCY.value,
                    retryable=True,
                )
        return None

    def _apply_event(
        self,
        envelope: SyncEventEnvelope,
        accepted: list[str],
        rejected: list[SyncRejectedEvent],
        conflicts: list[SyncConflict],
        server_agent_events: list[dict],
        *,
        insert_event_row: bool = True,
    ) -> None:
        """Projection-only application; `insert_event_row=False` replays
        already-stored events (used by scripts/rebuild_learner_projections.py)."""
        event_type = envelope.event_type
        if event_type == "ANSWER_SUBMITTED":
            self._apply_answer_submitted(envelope, accepted, rejected, conflicts,
                                         server_agent_events, insert_event_row=insert_event_row)
            return
        if event_type == "SESSION_COMPLETED":
            self._apply_session_completed(envelope, accepted, rejected, conflicts,
                                          insert_event_row=insert_event_row)
            return
        if event_type in (
            "DIAGNOSTIC_STARTED",
            "HINT_REQUESTED",
            "CONTENT_PRESENTED",
            "WORKED_EXAMPLE_PRESENTED",
            "MICRO_LESSON_PRESENTED",
            "DIAGNOSTIC_COMPLETED",
            "PLAN_READY",
        ):
            self._apply_observational(envelope, accepted, rejected, conflicts,
                                      insert_event_row=insert_event_row)
            return
        rejected.append(
            SyncRejectedEvent(
                event_id=envelope.event_id,
                code=SyncErrorCode.INVALID_SCHEMA.value,
                retryable=False,
            )
        )

    def _event_row(
        self, envelope: SyncEventEnvelope, received_at: str
    ) -> tuple:
        return (
            envelope.event_id,
            envelope.student_id,
            envelope.session_id,
            event_type_value(envelope),
            json.dumps(envelope.payload, sort_keys=True),
            envelope.policy_version,
            envelope.content_pack_version,
            envelope.device_occurred_at,
            received_at,
            envelope.device_id,
            envelope.device_sequence,
            "offline",
            envelope.integrity_hash,
        )

    # ------------------------------------------------------------------
    # Event applications
    # ------------------------------------------------------------------

    def _apply_observational(
        self,
        envelope: SyncEventEnvelope,
        accepted: list[str],
        rejected: list[SyncRejectedEvent],
        conflicts: list[SyncConflict],
        *,
        insert_event_row: bool = True,
    ) -> None:
        received_at = _utc_now_iso()
        with connect(self.db) as connection:
            with transaction(connection):
                if insert_event_row:
                    self._insert_learning_event_row(connection, envelope, received_at)
                self._ensure_session(connection, envelope, SessionState.NEW.value)
        accepted.append(envelope.event_id)

    def _apply_session_completed(
        self,
        envelope: SyncEventEnvelope,
        accepted: list[str],
        rejected: list[SyncRejectedEvent],
        conflicts: list[SyncConflict],
        *,
        insert_event_row: bool = True,
    ) -> None:
        received_at = _utc_now_iso()
        with connect(self.db) as connection:
            with transaction(connection):
                if insert_event_row:
                    self._insert_learning_event_row(connection, envelope, received_at)
                row = connection.execute(
                    "SELECT session_state FROM study_sessions WHERE session_id = ?",
                    (envelope.session_id,),
                ).fetchone()
                if row is None:
                    connection.execute(
                        """
                        INSERT INTO study_sessions (session_id, student_id, session_state, started_at, updated_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (envelope.session_id, envelope.student_id,
                         SessionState.SESSION_COMPLETED.value, received_at, received_at),
                    )
                elif row["session_state"] != SessionState.SESSION_COMPLETED.value:
                    connection.execute(
                        """
                        UPDATE study_sessions
                        SET session_state = ?, completed_at = ?, updated_at = ?
                        WHERE session_id = ?
                        """,
                        (SessionState.SESSION_COMPLETED.value, received_at, received_at,
                         envelope.session_id),
                    )
        accepted.append(envelope.event_id)

    def _apply_answer_submitted(
        self,
        envelope: SyncEventEnvelope,
        accepted: list[str],
        rejected: list[SyncRejectedEvent],
        conflicts: list[SyncConflict],
        server_agent_events: list[dict],
        *,
        insert_event_row: bool = True,
    ) -> None:
        question_id = envelope.question_id or envelope.payload.get("question_id")
        question_version = envelope.question_version or envelope.payload.get("question_version")
        selected = envelope.payload.get("selected_choice_id")
        if not question_id or question_version is None or not selected:
            rejected.append(
                SyncRejectedEvent(
                    event_id=envelope.event_id,
                    code=SyncErrorCode.INVALID_SCHEMA.value,
                    retryable=False,
                )
            )
            return

        try:
            correct = self.answer_keys.score(
                pack_version=envelope.content_pack_version,
                question_id=question_id,
                question_version=int(question_version),
                selected_choice_id=selected,
            )
            meta = self.answer_keys.pack(envelope.content_pack_version).item_meta(
                question_id, int(question_version)
            )
        except Exception as exc:
            code = getattr(exc, "code", SyncErrorCode.QUESTION_VERSION_UNKNOWN)
            rejected.append(
                SyncRejectedEvent(
                    event_id=envelope.event_id,
                    code=str(code.value),
                    retryable=False,
                )
            )
            return

        received_at = _utc_now_iso()
        with connect(self.db) as connection:
            with transaction(connection):
                if insert_event_row:
                    self._insert_learning_event_row(connection, envelope, received_at)
                self._ensure_session(connection, envelope, SessionState.QUESTION_ACTIVE.value)

                existing_attempt = connection.execute(
                    "SELECT 1 FROM answer_attempts WHERE event_id = ?",
                    (envelope.event_id,),
                ).fetchone()
                if existing_attempt is not None:
                    accepted.append(envelope.event_id)
                    return

                attempt_id = envelope.payload.get("attempt_id") or f"att_{envelope.event_id[:16]}"
                prior_same_attempt = connection.execute(
                    """
                    SELECT COUNT(*) AS total FROM answer_attempts
                    WHERE session_id = ? AND attempt_id = ?
                    """,
                    (envelope.session_id, attempt_id),
                ).fetchone()["total"]
                if prior_same_attempt > 0:
                    stored_attempt_id = f"{attempt_id}#dup{prior_same_attempt}"
                else:
                    stored_attempt_id = attempt_id

                session_row = connection.execute(
                    "SELECT session_state FROM study_sessions WHERE session_id = ?",
                    (envelope.session_id,),
                ).fetchone()
                late_event = bool(
                    session_row
                    and session_row["session_state"] == SessionState.SESSION_COMPLETED.value
                )

                prior_same_item = connection.execute(
                    """
                    SELECT COUNT(*) AS total FROM answer_attempts
                    WHERE session_id = ? AND content_id = ? AND validity = 'valid'
                    """,
                    (envelope.session_id, question_id),
                ).fetchone()["total"]

                weight = self._evidence_weight(meta, envelope, repeated=prior_same_item > 0)
                if prior_same_attempt > 0:
                    validity = "non_scoring_duplicate"
                    weight = 0.0
                    conflicts.append(
                        SyncConflict(
                            event_id=envelope.event_id,
                            conflict_type=ConflictType.ATTEMPT_ALREADY_SCORED.value,
                            detail=f"attempt {attempt_id} already scored in session {envelope.session_id}",
                        )
                    )
                    self._record_conflict(
                        connection, envelope, ConflictType.ATTEMPT_ALREADY_SCORED,
                        f"attempt {attempt_id} already scored",
                    )
                else:
                    validity = "valid"

                misconception = None
                if not correct and validity == "valid":
                    misconception = meta["misconception_map"].get(selected)

                connection.execute(
                    """
                    INSERT INTO answer_attempts (
                        attempt_id, event_id, student_id, session_id, content_id,
                        version, sequence, selected_choice_id, correct, hint_level,
                        weight, validity, occurred_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        stored_attempt_id,
                        envelope.event_id,
                        envelope.student_id,
                        envelope.session_id,
                        question_id,
                        int(question_version),
                        0,
                        selected,
                        1 if correct else 0,
                        int(envelope.payload.get("hint_level", 0)),
                        weight,
                        validity,
                        envelope.device_occurred_at or received_at,
                    ),
                )

                if validity == "valid":
                    if meta["skill"]:
                        self._update_skill_state(
                            connection, envelope.student_id, meta["skill"],
                            correct, weight, received_at,
                        )

                    if misconception:
                        self._record_misconception_evidence(
                            connection, envelope, question_id, int(question_version),
                            meta, misconception, received_at,
                        )

                    if prior_same_item > 0:
                        conflicts.append(
                            SyncConflict(
                                event_id=envelope.event_id,
                                conflict_type=ConflictType.PARALLEL_ATTEMPT_DETECTED.value,
                                detail=f"repeated attempt on {question_id} in session {envelope.session_id}",
                            )
                        )
                        self._record_conflict(
                            connection, envelope, ConflictType.PARALLEL_ATTEMPT_DETECTED,
                            conflict_detail(envelope, question_id),
                        )

                    if late_event:
                        conflicts.append(
                            SyncConflict(
                                event_id=envelope.event_id,
                                conflict_type=ConflictType.SUMMARY_REVISED.value,
                                detail=f"late event after session {envelope.session_id} completed",
                            )
                        )
                        self._record_conflict(
                            connection, envelope, ConflictType.SUMMARY_REVISED,
                            f"late event after session {envelope.session_id} completed",
                        )
                    else:
                        self._transition_session(
                            connection, envelope.session_id,
                            SessionState.ANSWER_EVALUATED, received_at,
                        )

        accepted.append(envelope.event_id)
        server_agent_events.append(
            {
                "source_event_id": envelope.event_id,
                "action": "answer_evaluated_offline",
                "action_payload": {"question_id": question_id, "correct": correct},
                "reason_code": "offline-version-bound-scoring",
                "reason_text": "Re-scored offline answer against referenced question version",
            }
        )

    def _evidence_weight(self, meta: dict, envelope: SyncEventEnvelope, repeated: bool) -> float:
        difficulty = meta.get("difficulty") or 2
        hint_level = int(envelope.payload.get("hint_level", 0))
        difficulty_weight = {1: 1.0, 2: 1.0, 3: 1.25}.get(difficulty, 1.0)
        hint_multiplier = {0: 1.0, 1: 0.85, 2: 0.6, 3: 0.3}.get(hint_level, 1.0)
        repeat_multiplier = 0.5 if repeated else 1.0
        return round(difficulty_weight * hint_multiplier * repeat_multiplier, 4)

    def _update_skill_state(
        self,
        connection,
        student_id: str,
        skill: str,
        correct: bool,
        weight: float,
        now: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO student_skill_states (
                student_id, skill, alpha, beta, mastery, confidence,
                evidence_count, correct_streak, incorrect_streak,
                last_practiced_at, review_due_at, projection_origin, updated_at
            ) VALUES (?, ?, 2.0, 2.0, 0.5, 0.0, 0, 0, 0, NULL, NULL, 'sync', ?)
            ON CONFLICT(student_id, skill) DO NOTHING
            """,
            (student_id, skill, now),
        )
        row = connection.execute(
            """
            SELECT alpha, beta, evidence_count, correct_streak, incorrect_streak,
                   last_practiced_at, review_due_at
            FROM student_skill_states WHERE student_id = ? AND skill = ?
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
        state.record_attempt(correct, weight, now)
        connection.execute(
            """
            UPDATE student_skill_states
            SET alpha = ?, beta = ?, mastery = ?, confidence = ?,
                evidence_count = ?, correct_streak = ?, incorrect_streak = ?,
                last_practiced_at = ?, updated_at = ?
            WHERE student_id = ? AND skill = ?
            """,
            (
                state.alpha, state.beta, state.mastery, state.confidence,
                state.evidence_count, state.correct_streak, state.incorrect_streak,
                state.last_practiced_at, now, student_id, skill,
            ),
        )

    def _record_misconception_evidence(
        self,
        connection,
        envelope: SyncEventEnvelope,
        question_id: str,
        question_version: int,
        meta: dict,
        misconception: str,
        now: str,
    ) -> None:
        evidence_id = f"evid_{uuid.uuid4().hex[:12]}"
        connection.execute(
            """
            INSERT INTO misconception_evidence (
                evidence_id, student_id, session_id, event_id, skill, subskill,
                misconception, source_label, confidence_label, state,
                item_id, item_version, observed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'offline_distractor', 'high',
                      'confirmed_offline', ?, ?, ?)
            """,
            (
                evidence_id,
                envelope.student_id,
                envelope.session_id,
                envelope.event_id,
                meta["skill"],
                meta.get("subskill"),
                misconception,
                question_id,
                question_version,
                now,
            ),
        )

    def _record_conflict(
        self,
        connection,
        envelope: SyncEventEnvelope,
        conflict_type: ConflictType,
        detail: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO sync_conflicts (
                conflict_id, event_id, student_id, session_id, conflict_type,
                detail_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"cf_{uuid.uuid4().hex[:12]}",
                envelope.event_id,
                envelope.student_id,
                envelope.session_id,
                conflict_type.value,
                json.dumps({"detail": detail}),
                _utc_now_iso(),
            ),
        )

    def _ensure_session(
        self,
        connection,
        envelope: SyncEventEnvelope,
        default_state: str,
    ) -> None:
        row = connection.execute(
            "SELECT 1 FROM study_sessions WHERE session_id = ?",
            (envelope.session_id,),
        ).fetchone()
        if row is None:
            connection.execute(
                """
                INSERT INTO study_sessions (session_id, student_id, session_state, started_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (envelope.session_id, envelope.student_id, default_state,
                 envelope.device_occurred_at or _utc_now_iso(), _utc_now_iso()),
            )

    def _transition_session(
        self,
        connection,
        session_id: str,
        target: SessionState,
        now: str,
    ) -> None:
        row = connection.execute(
            "SELECT session_state FROM study_sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if row is None or row["session_state"] == SessionState.SESSION_COMPLETED.value:
            return
        if row["session_state"] != target.value:
            connection.execute(
                """
                UPDATE study_sessions SET session_state = ?, updated_at = ? WHERE session_id = ?
                """,
                (target.value, now, session_id),
            )

    def _insert_learning_event_row(
        self,
        connection,
        envelope: SyncEventEnvelope,
        received_at: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO learning_events (
                event_id, student_id, session_id, event_type, payload_json,
                policy_version, content_version, occurred_at, received_at,
                device_id, device_sequence, origin, integrity_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'offline', ?)
            """,
            self._event_row(envelope, received_at)[:11] + (envelope.integrity_hash,),
        )

    # ------------------------------------------------------------------
    # Snapshot
    # ------------------------------------------------------------------

    def build_snapshot(self, student_id: str) -> SnapshotResponse:
        with connect(self.db) as connection:
            student_row = connection.execute(
                "SELECT * FROM students WHERE id = ?", (student_id,)
            ).fetchone()
            if student_row is None:
                raise KeyError(f"Unknown student {student_id}")
            skill_rows = connection.execute(
                "SELECT * FROM student_skill_states WHERE student_id = ?",
                (student_id,),
            ).fetchall()
            session_row = connection.execute(
                """
                SELECT * FROM study_sessions WHERE student_id = ?
                ORDER BY updated_at DESC LIMIT 1
                """,
                (student_id,),
            ).fetchone()
            plan_row = connection.execute(
                """
                SELECT * FROM study_plans WHERE student_id = ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (student_id,),
            ).fetchone()
            event_count = connection.execute(
                "SELECT COUNT(*) AS total FROM learning_events WHERE student_id = ?",
                (student_id,),
            ).fetchone()["total"]
            latest = connection.execute(
                """
                SELECT MAX(rowid) AS max_rowid FROM learning_events
                """
            ).fetchone()["max_rowid"]

        skill_states = [
            {
                "skill": row["skill"],
                "mastery": row["mastery"],
                "confidence": row["confidence"],
                "evidence_count": row["evidence_count"],
            }
            for row in skill_rows
        ]
        session = None
        if session_row is not None:
            session = {
                "session_id": session_row["session_id"],
                "session_state": session_row["session_state"],
                "started_at": session_row["started_at"],
                "completed_at": session_row["completed_at"],
            }
        plan = None
        if plan_row is not None:
            plan = {"plan_json": json.loads(plan_row["plan_json"])}

        strategy_memory = {
            "intervention_stats": self._intervention_stats(student_id),
            "facts": self._facts_summary(student_id),
        }

        return SnapshotResponse(
            student={
                "id": student_row["id"],
                "name": student_row["name"],
                "daily_minutes": student_row["daily_minutes"],
                "target_score": student_row["target_score"],
                "mastery": json.loads(student_row["mastery_json"]),
            },
            skill_states=skill_states,
            session=session,
            plan=plan,
            strategy_memory=strategy_memory,
            content_pack_versions=self.answer_keys.list_versions(),
            snapshot_version=event_count,
            server_cursor=f"cursor_{latest or 0}",
        )

    def _intervention_stats(self, student_id: str) -> list[dict]:
        with connect(self.db) as connection:
            rows = connection.execute(
                """
                SELECT skill, misconception, intervention, difficulty_band,
                       immediate_correct, immediate_attempts, immediate_weight,
                       short_term_correct, short_term_attempts, short_term_weight,
                       delayed_correct, delayed_attempts, delayed_weight
                FROM intervention_stats WHERE student_id = ?
                """,
                (student_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def _facts_summary(self, student_id: str) -> list[dict]:
        try:
            facts = self.memory.get_facts(student_id)
        except Exception:
            return []
        return [
            {
                "fact_id": fact.fact_id,
                "key": fact.key,
                "value": fact.value,
                "confidence": fact.confidence,
            }
            for fact in facts[:20]
        ]


def event_type_value(envelope: SyncEventEnvelope) -> str:
    return envelope.event_type


def conflict_detail(envelope: SyncEventEnvelope, question_id: str) -> str:
    return f"parallel attempt on {question_id} by device {envelope.device_id}"
