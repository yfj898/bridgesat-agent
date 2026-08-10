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
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from functools import lru_cache

import psycopg
from psycopg.errors import UniqueViolation

from app.domain.learner import SkillState
from app.domain.sessions import SessionState
from app.infrastructure.event_store import EventStore
from app.infrastructure.learner_store import LearnerStore
from app.infrastructure.pg import transaction
from app.memory.outbox import student_advisory_lock
from app.memory.pg_memory import PGMemory

from .protocol import (
    MAX_EVENTS_PER_BATCH,
    MAX_PAYLOAD_BYTES,
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
from .versioned_scoring import QuestionVersionError, VersionedAnswerKey


_GLOBAL_ID_CONSTRAINTS = frozenset(
    {
        "learning_events_pkey",
        "answer_attempts_pkey",
        "answer_attempts_event_id_key",
        "study_sessions_pkey",
    }
)
_LEARNING_EVENT_ID_CONSTRAINTS = frozenset({"learning_events_pkey"})
_ANSWER_EVENT_ID_CONSTRAINTS = frozenset({"answer_attempts_event_id_key"})
_ANSWER_ATTEMPT_ID_CONSTRAINTS = frozenset({"answer_attempts_pkey"})


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


@contextmanager
def _transaction_scope(
    connection: psycopg.Connection,
    *,
    in_transaction: bool,
) -> Iterator[psycopg.Connection]:
    if in_transaction:
        yield connection
    else:
        with transaction(connection) as scoped:
            yield scoped


@contextmanager
def _event_savepoint(connection: psycopg.Connection, name: str) -> Iterator[None]:
    connection.execute(f"SAVEPOINT {name}")
    try:
        yield
    except BaseException:
        try:
            connection.execute(f"ROLLBACK TO SAVEPOINT {name}")
        except BaseException:
            pass
        try:
            connection.execute(f"RELEASE SAVEPOINT {name}")
        except BaseException:
            pass
        raise
    else:
        connection.execute(f"RELEASE SAVEPOINT {name}")


class DeviceNotFoundError(RuntimeError):
    pass


class DeviceRevokedError(RuntimeError):
    pass


class StudentInactiveError(ValueError):
    pass


class EventValidationError(ValueError):
    """A deterministic event-domain failure safe to reject at its savepoint."""

    def __init__(
        self,
        message: str,
        *,
        code: SyncErrorCode = SyncErrorCode.INTERNAL_RETRYABLE,
        retryable: bool = True,
    ) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(message)


class SyncService:
    def __init__(self, connection: psycopg.Connection) -> None:
        self.connection = connection
        self.events = EventStore(connection)
        self.learner = LearnerStore(connection)
        self.memory = PGMemory(connection)
        self.answer_keys = VersionedAnswerKey()

    @staticmethod
    @lru_cache(maxsize=1024)
    def _cached_student_lock(student_id: str) -> threading.Lock:
        """Compatibility optimization; PostgreSQL is the correctness lock."""
        return threading.Lock()

    def _student_lock(self, student_id: str) -> threading.Lock:
        return self._cached_student_lock(student_id)

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
            raise KeyError(f"Unknown student {student_id}")
        if row["status"] != "active":
            raise StudentInactiveError(
                f"Student {student_id} is not active (status={row['status']})"
            )
        deletion = self.connection.execute(
            """
            SELECT state
            FROM student_deletions
            WHERE student_id = %s
              AND tenant_id = current_setting('app.tenant_id', true)
            FOR UPDATE
            """,
            (student_id,),
        ).fetchone()
        if deletion is not None:
            raise StudentInactiveError(
                f"Student {student_id} has a deletion state ({deletion['state']})"
            )

    # ------------------------------------------------------------------
    # Devices
    # ------------------------------------------------------------------

    def register_device(
        self,
        student_id: str,
        device_name: str | None,
        device_id: str | None = None,
    ) -> DeviceRegistration:
        with student_advisory_lock(self.connection, student_id):
            device_id = device_id or f"dev_{uuid.uuid4().hex[:12]}"
            now = _utc_now_iso()
            with transaction(self.connection):
                self._require_active_student(student_id)
                self.connection.execute(
                    """
                    INSERT INTO devices (
                        tenant_id, device_id, student_id, device_name, status, created_at
                    ) VALUES (
                        current_setting('app.tenant_id'), %s, %s, %s, 'active', %s
                    )
                    """,
                    (device_id, student_id, device_name, now),
                )
        return DeviceRegistration(device_id=device_id, student_id=student_id, status="active")

    def revoke_device(self, device_id: str, student_id: str) -> None:
        now = _utc_now_iso()
        with student_advisory_lock(self.connection, student_id):
            with transaction(self.connection):
                try:
                    self._require_active_student(student_id)
                except KeyError as exc:
                    raise DeviceNotFoundError(
                        f"Device {device_id} not found"
                    ) from exc
                cursor = self.connection.execute(
                    """
                    UPDATE devices
                    SET status = 'revoked', revoked_at = %s
                    WHERE device_id = %s
                      AND student_id = %s
                      AND tenant_id = current_setting('app.tenant_id', true)
                    """,
                    (now, device_id, student_id),
                )
                if cursor.rowcount == 0:
                    raise DeviceNotFoundError(f"Device {device_id} not found")

    def _student_exists(
        self,
        student_id: str,
        *,
        in_transaction: bool = False,
    ) -> bool:
        try:
            with _transaction_scope(
                self.connection,
                in_transaction=in_transaction,
            ):
                self._require_active_student(student_id)
            return True
        except (KeyError, StudentInactiveError):
            return False

    def _verify_device(
        self,
        device_id: str,
        student_id: str,
        *,
        in_transaction: bool = False,
    ) -> None:
        with _transaction_scope(
            self.connection,
            in_transaction=in_transaction,
        ):
            try:
                self._require_active_student(student_id)
            except KeyError as exc:
                raise DeviceNotFoundError(
                    f"Device {device_id} not registered"
                ) from exc
            row = self.connection.execute(
                """
                SELECT status
                FROM devices
                WHERE device_id = %s
                  AND student_id = %s
                  AND tenant_id = current_setting('app.tenant_id', true)
                FOR UPDATE
                """,
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
        if len(request.events) > MAX_EVENTS_PER_BATCH:
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
        with student_advisory_lock(self.connection, request.student_id):
            try:
                with transaction(self.connection):
                    return self._process_batch_locked(request, in_transaction=True)
            except StudentInactiveError:
                return self._unauthorized_student_response()

    @staticmethod
    def _unauthorized_student_response() -> SyncResponse:
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

    def _process_batch_locked(
        self,
        request: SyncRequest,
        *,
        in_transaction: bool = False,
    ) -> SyncResponse:
        if not self._student_exists(request.student_id, in_transaction=in_transaction):
            return self._unauthorized_student_response()
        self._verify_device(
            request.device_id,
            request.student_id,
            in_transaction=in_transaction,
        )

        accepted: list[str] = []
        duplicates: list[str] = []
        rejected: list[SyncRejectedEvent] = []
        conflicts: list[SyncConflict] = []
        server_agent_events: list[dict] = []
        accepted_max_sequence = 0

        for event_index, envelope in enumerate(request.events):
            envelope = envelope.model_copy(
                update={
                    "student_id": request.student_id,
                    "device_id": request.device_id,
                }
            )
            if not self._verify_integrity(envelope):
                rejected.append(
                    SyncRejectedEvent(
                        event_id=envelope.event_id,
                        code=SyncErrorCode.INVALID_SCHEMA.value,
                        retryable=False,
                    )
                )
                continue
            payload_bytes = len(json.dumps(envelope.payload, sort_keys=True).encode("utf-8"))
            if payload_bytes > MAX_PAYLOAD_BYTES:
                rejected.append(
                    SyncRejectedEvent(
                        event_id=envelope.event_id,
                        code=SyncErrorCode.PAYLOAD_TOO_LARGE.value,
                        retryable=False,
                    )
                )
                continue
            if self.events.learning_event_exists(
                envelope.event_id,
                student_id=request.student_id,
            ):
                duplicates.append(envelope.event_id)
                continue
            if not self._sequence_increases(
                request, envelope, in_transaction=in_transaction
            ):
                rejected.append(
                    SyncRejectedEvent(
                        event_id=envelope.event_id,
                        code=SyncErrorCode.INVALID_SCHEMA.value,
                        retryable=False,
                    )
                )
                continue

            try:
                dependency_error = self._missing_dependency(request.student_id, envelope)
            except EventValidationError as exc:
                rejected.append(
                    SyncRejectedEvent(
                        event_id=envelope.event_id,
                        code=exc.code.value,
                        retryable=exc.retryable,
                    )
                )
                continue
            if dependency_error is not None:
                rejected.append(dependency_error)
                continue

            accepted_length = len(accepted)
            rejected_length = len(rejected)
            conflicts_length = len(conflicts)
            server_agent_events_length = len(server_agent_events)
            try:
                if in_transaction:
                    with _event_savepoint(self.connection, f"sync_event_{event_index}"):
                        self._apply_event(
                            envelope,
                            accepted,
                            rejected,
                            conflicts,
                            server_agent_events,
                            in_transaction=True,
                        )
                else:
                    self._apply_event(
                        envelope,
                        accepted,
                        rejected,
                        conflicts,
                        server_agent_events,
                    )
            except (DeviceNotFoundError, DeviceRevokedError):
                raise
            except EventValidationError as exc:
                del accepted[accepted_length:]
                del rejected[rejected_length:]
                del conflicts[conflicts_length:]
                del server_agent_events[server_agent_events_length:]
                rejected.append(
                    SyncRejectedEvent(
                        event_id=envelope.event_id,
                        code=exc.code.value,
                        retryable=exc.retryable,
                    )
                )
            except UniqueViolation as unique_error:
                try:
                    collision = self._global_id_collision(envelope, unique_error)
                except BaseException:
                    raise unique_error
                if collision is None:
                    raise unique_error
                del accepted[accepted_length:]
                del rejected[rejected_length:]
                del conflicts[conflicts_length:]
                del server_agent_events[server_agent_events_length:]
                rejected.append(
                    SyncRejectedEvent(
                        event_id=envelope.event_id,
                        code=collision.code.value,
                        retryable=collision.retryable,
                    )
                )
            if envelope.event_id in accepted:
                accepted_max_sequence = max(
                    accepted_max_sequence, envelope.device_sequence
                )

        if accepted:
            self._advance_device_sequence(
                request.device_id,
                request.student_id,
                accepted_max_sequence,
                in_transaction=in_transaction,
            )
        snapshot = self.build_snapshot(
            request.student_id,
            in_transaction=in_transaction,
        )
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
            return False
        digest = hashlib.sha256()
        digest.update(envelope.event_type.encode("utf-8"))
        digest.update(b"\x00")
        canonical = json.dumps(envelope.payload, sort_keys=True, separators=(",", ":"))
        digest.update(canonical.encode("utf-8"))
        return envelope.integrity_hash == f"sha256:{digest.hexdigest()}"

    def _sequence_increases(
        self,
        request: SyncRequest,
        envelope: SyncEventEnvelope,
        *,
        in_transaction: bool = False,
    ) -> bool:
        """SYNC_PROTOCOL rule: `device_sequence` increases per device.

        The server tracks the last accepted sequence per device
        (`devices.last_device_sequence`) and rejects events at or below it,
        so a replayed or reordered batch cannot rewrite history.
        Authoritative scope is the request (token-verified) device, not the
        client-claims envelope fields.
        """
        with _transaction_scope(
            self.connection,
            in_transaction=in_transaction,
        ):
            row = self.connection.execute(
                """
                SELECT last_device_sequence
                FROM devices
                WHERE device_id = %s
                  AND student_id = %s
                  AND tenant_id = current_setting('app.tenant_id', true)
                FOR UPDATE
                """,
                (request.device_id, request.student_id),
            ).fetchone()
        if row is None:
            return False
        return envelope.device_sequence > row["last_device_sequence"]

    def _advance_device_sequence(
        self,
        device_id: str,
        student_id: str,
        batch_max: int,
        *,
        in_transaction: bool = False,
    ) -> None:
        with _transaction_scope(
            self.connection,
            in_transaction=in_transaction,
        ):
            cursor = self.connection.execute(
                """
                UPDATE devices
                SET last_device_sequence = %s
                WHERE device_id = %s
                  AND student_id = %s
                  AND tenant_id = current_setting('app.tenant_id', true)
                """,
                (batch_max, device_id, student_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(
                    "device sequence update expected exactly one tenant-scoped row"
                )

    def _missing_dependency(
        self, student_id: str, envelope: SyncEventEnvelope
    ) -> SyncRejectedEvent | None:
        for dep in envelope.depends_on_event_ids:
            if self.events.learning_event_exists(dep, student_id=student_id):
                continue
            owner = self.events.learning_event_owner(dep)
            if owner is not None and owner != student_id:
                raise EventValidationError(
                    f"Dependency {dep} belongs to another student",
                    code=SyncErrorCode.INVALID_SCHEMA,
                    retryable=False,
                )
            if owner is None:
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
        in_transaction: bool = False,
    ) -> None:
        """Projection-only application; `insert_event_row=False` replays
        already-stored events (used by scripts/rebuild_learner_projections.py)."""
        event_type = envelope.event_type
        if event_type == "ANSWER_SUBMITTED":
            self._apply_answer_submitted(envelope, accepted, rejected, conflicts,
                                         server_agent_events,
                                         insert_event_row=insert_event_row,
                                         in_transaction=in_transaction)
            return
        if event_type == "SESSION_COMPLETED":
            self._apply_session_completed(envelope, accepted, rejected, conflicts,
                                          insert_event_row=insert_event_row,
                                          in_transaction=in_transaction)
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
                                      insert_event_row=insert_event_row,
                                      in_transaction=in_transaction)
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
        in_transaction: bool = False,
    ) -> None:
        received_at = _utc_now_iso()
        with _transaction_scope(
            self.connection,
            in_transaction=in_transaction,
        ):
            if insert_event_row:
                self._insert_learning_event_row(self.connection, envelope, received_at)
            self._ensure_session(self.connection, envelope, SessionState.NEW.value)
        accepted.append(envelope.event_id)

    def _apply_session_completed(
        self,
        envelope: SyncEventEnvelope,
        accepted: list[str],
        rejected: list[SyncRejectedEvent],
        conflicts: list[SyncConflict],
        *,
        insert_event_row: bool = True,
        in_transaction: bool = False,
    ) -> None:
        received_at = _utc_now_iso()
        with _transaction_scope(
            self.connection,
            in_transaction=in_transaction,
        ):
            if insert_event_row:
                self._insert_learning_event_row(self.connection, envelope, received_at)
            row = self._locked_session(
                self.connection, envelope.session_id, envelope.student_id
            )
            if row is None:
                self.connection.execute(
                    """
                    INSERT INTO study_sessions (
                        tenant_id, session_id, student_id, session_state,
                        started_at, updated_at
                    ) VALUES (
                        current_setting('app.tenant_id'), %s, %s, %s, %s, %s
                    )
                    """,
                    (envelope.session_id, envelope.student_id,
                     SessionState.SESSION_COMPLETED.value, received_at, received_at),
                )
            elif row["session_state"] != SessionState.SESSION_COMPLETED.value:
                self.connection.execute(
                    """
                    UPDATE study_sessions
                    SET session_state = %s, completed_at = %s, updated_at = %s
                    WHERE session_id = %s
                      AND student_id = %s
                      AND tenant_id = current_setting('app.tenant_id', true)
                    """,
                    (SessionState.SESSION_COMPLETED.value, received_at, received_at,
                     envelope.session_id, envelope.student_id),
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
        in_transaction: bool = False,
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
        except QuestionVersionError:
            rejected.append(
                SyncRejectedEvent(
                    event_id=envelope.event_id,
                    code=SyncErrorCode.QUESTION_VERSION_UNKNOWN.value,
                    retryable=False,
                )
            )
            return

        received_at = _utc_now_iso()
        connection = self.connection
        with _transaction_scope(
            connection,
            in_transaction=in_transaction,
        ):
                if insert_event_row:
                    self._insert_learning_event_row(connection, envelope, received_at)
                self._ensure_session(connection, envelope, SessionState.QUESTION_ACTIVE.value)

                existing_attempt = connection.execute(
                    """
                    SELECT 1
                    FROM answer_attempts
                    WHERE event_id = %s
                      AND student_id = %s
                      AND tenant_id = current_setting('app.tenant_id', true)
                    """,
                    (envelope.event_id, envelope.student_id),
                ).fetchone()
                if existing_attempt is not None:
                    accepted.append(envelope.event_id)
                    return

                attempt_id = envelope.payload.get("attempt_id") or f"att_{envelope.event_id[:16]}"
                attempt_owner = self._attempt_owner(connection, attempt_id)
                if attempt_owner is not None and attempt_owner != envelope.student_id:
                    raise EventValidationError(
                        f"Attempt {attempt_id} belongs to another student",
                        code=SyncErrorCode.INVALID_SCHEMA,
                        retryable=False,
                    )
                prior_same_attempt = connection.execute(
                    """
                    SELECT COUNT(*) AS total FROM answer_attempts
                    WHERE session_id = %s
                      AND student_id = %s
                      AND tenant_id = current_setting('app.tenant_id', true)
                      AND attempt_id = %s
                    """,
                    (envelope.session_id, envelope.student_id, attempt_id),
                ).fetchone()["total"]
                if prior_same_attempt > 0:
                    stored_attempt_id = f"{attempt_id}#dup{prior_same_attempt}"
                else:
                    stored_attempt_id = attempt_id

                session_row = self._locked_session(
                    connection, envelope.session_id, envelope.student_id
                )
                late_event = bool(
                    session_row
                    and session_row["session_state"] == SessionState.SESSION_COMPLETED.value
                )

                prior_same_item = connection.execute(
                    """
                    SELECT COUNT(*) AS total FROM answer_attempts
                    WHERE session_id = %s
                      AND student_id = %s
                      AND tenant_id = current_setting('app.tenant_id', true)
                      AND content_id = %s
                      AND validity = 'valid'
                    """,
                    (envelope.session_id, envelope.student_id, question_id),
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
                        tenant_id, attempt_id, event_id, student_id, session_id,
                        content_id, version, sequence, selected_choice_id, correct,
                        hint_level, weight, validity, occurred_at
                    ) VALUES (
                        current_setting('app.tenant_id'), %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s
                    )
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
                            connection, envelope.student_id, envelope.session_id,
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
                tenant_id, student_id, skill, alpha, beta, mastery, confidence,
                evidence_count, correct_streak, incorrect_streak,
                last_practiced_at, review_due_at, projection_origin, updated_at
            ) VALUES (
                current_setting('app.tenant_id'), %s, %s, 2.0, 2.0, 0.5, 0.0,
                0, 0, 0, NULL, NULL, 'sync', %s
            )
            ON CONFLICT(student_id, skill) DO NOTHING
            """,
            (student_id, skill, now),
        )
        row = connection.execute(
            """
            SELECT alpha, beta, evidence_count, correct_streak, incorrect_streak,
                   last_practiced_at, review_due_at
            FROM student_skill_states
            WHERE student_id = %s
              AND tenant_id = current_setting('app.tenant_id', true)
              AND skill = %s
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
            SET alpha = %s, beta = %s, mastery = %s, confidence = %s,
                evidence_count = %s, correct_streak = %s, incorrect_streak = %s,
                last_practiced_at = %s, updated_at = %s
            WHERE student_id = %s
              AND tenant_id = current_setting('app.tenant_id', true)
              AND skill = %s
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
                tenant_id, evidence_id, student_id, session_id, event_id, skill,
                subskill, misconception, source_label, confidence_label, state,
                item_id, item_version, observed_at
            ) VALUES (
                current_setting('app.tenant_id'), %s, %s, %s, %s, %s, %s, %s,
                'offline_distractor', 'high', 'confirmed_offline', %s, %s, %s
            )
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
                tenant_id, conflict_id, event_id, student_id, session_id,
                conflict_type, detail_json, created_at
            ) VALUES (
                current_setting('app.tenant_id'), %s, %s, %s, %s, %s, %s, %s
            )
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
        row = self._locked_session(connection, envelope.session_id, envelope.student_id)
        if row is None:
            connection.execute(
                """
                INSERT INTO study_sessions (
                    tenant_id, session_id, student_id, session_state,
                    started_at, updated_at
                ) VALUES (
                    current_setting('app.tenant_id'), %s, %s, %s, %s, %s
                )
                """,
                (envelope.session_id, envelope.student_id, default_state,
                 envelope.device_occurred_at or _utc_now_iso(), _utc_now_iso()),
            )

    def _transition_session(
        self,
        connection,
        student_id: str,
        session_id: str,
        target: SessionState,
        now: str,
    ) -> None:
        row = self._locked_session(connection, session_id, student_id)
        if row is None or row["session_state"] == SessionState.SESSION_COMPLETED.value:
            return
        if row["session_state"] != target.value:
            connection.execute(
                """
                UPDATE study_sessions
                SET session_state = %s, updated_at = %s
                WHERE session_id = %s
                  AND student_id = %s
                  AND tenant_id = current_setting('app.tenant_id', true)
                """,
                (target.value, now, session_id, student_id),
            )

    def _locked_session(
        self,
        connection,
        session_id: str,
        student_id: str,
    ) -> dict | None:
        """Lock a tenant session and reject a mismatched student owner."""
        row = connection.execute(
            """
            SELECT student_id, session_state,
                   (student_id = %s) AS owned_by_student
            FROM study_sessions
            WHERE session_id = %s
              AND tenant_id = current_setting('app.tenant_id', true)
            FOR UPDATE
            """,
            (student_id, session_id),
        ).fetchone()
        if row is not None and not row["owned_by_student"]:
            raise EventValidationError(
                f"Session {session_id} belongs to another student",
                code=SyncErrorCode.INVALID_SCHEMA,
                retryable=False,
            )
        return row

    def _attempt_owner(self, connection, attempt_id: str) -> str | None:
        row = connection.execute(
            """
            SELECT student_id
            FROM answer_attempts
            WHERE attempt_id = %s
              AND tenant_id = current_setting('app.tenant_id', true)
            FOR UPDATE
            """,
            (attempt_id,),
        ).fetchone()
        return None if row is None else row["student_id"]

    def _session_owner(self, connection, session_id: str) -> str | None:
        row = connection.execute(
            """
            SELECT student_id
            FROM study_sessions
            WHERE session_id = %s
              AND tenant_id = current_setting('app.tenant_id', true)
            FOR UPDATE
            """,
            (session_id,),
        ).fetchone()
        return None if row is None else row["student_id"]

    def _attempt_event_owner(self, connection, event_id: str) -> str | None:
        row = connection.execute(
            """
            SELECT student_id
            FROM answer_attempts
            WHERE event_id = %s
              AND tenant_id = current_setting('app.tenant_id', true)
            FOR UPDATE
            """,
            (event_id,),
        ).fetchone()
        return None if row is None else row["student_id"]

    def _global_id_collision(
        self,
        envelope: SyncEventEnvelope,
        unique_error: UniqueViolation,
    ) -> EventValidationError | None:
        constraint_name = getattr(unique_error.diag, "constraint_name", None)
        if constraint_name in _LEARNING_EVENT_ID_CONSTRAINTS:
            event_owner = self.events.learning_event_owner(envelope.event_id)
            if event_owner is not None:
                if event_owner != envelope.student_id:
                    return EventValidationError(
                        f"Learning event {envelope.event_id} belongs to another student",
                        code=SyncErrorCode.INVALID_SCHEMA,
                        retryable=False,
                    )
                return None
            return EventValidationError(
                "Global sync identifier is already owned outside the current tenant",
                code=SyncErrorCode.INVALID_SCHEMA,
                retryable=False,
            )

        if constraint_name in _ANSWER_EVENT_ID_CONSTRAINTS:
            event_owner = self.events.learning_event_owner(envelope.event_id)
            if event_owner is None:
                event_owner = self._attempt_event_owner(
                    self.connection, envelope.event_id
                )
            if event_owner is not None:
                if event_owner != envelope.student_id:
                    return EventValidationError(
                        f"Learning event {envelope.event_id} belongs to another student",
                        code=SyncErrorCode.INVALID_SCHEMA,
                        retryable=False,
                    )
                return None
            return EventValidationError(
                "Global sync identifier is already owned outside the current tenant",
                code=SyncErrorCode.INVALID_SCHEMA,
                retryable=False,
            )

        if constraint_name in _ANSWER_ATTEMPT_ID_CONSTRAINTS:
            if envelope.event_type != "ANSWER_SUBMITTED":
                return None
            attempt_id = envelope.payload.get("attempt_id") or f"att_{envelope.event_id[:16]}"
            attempt_owner = self._attempt_owner(self.connection, attempt_id)
            if attempt_owner is not None:
                if attempt_owner != envelope.student_id:
                    return EventValidationError(
                        f"Attempt {attempt_id} belongs to another student",
                        code=SyncErrorCode.INVALID_SCHEMA,
                        retryable=False,
                    )
                return None
            return EventValidationError(
                "Global sync identifier is already owned outside the current tenant",
                code=SyncErrorCode.INVALID_SCHEMA,
                retryable=False,
            )

        if constraint_name == "study_sessions_pkey":
            session_owner = self._session_owner(self.connection, envelope.session_id)
            if session_owner is not None:
                if session_owner != envelope.student_id:
                    return EventValidationError(
                        f"Session {envelope.session_id} belongs to another student",
                        code=SyncErrorCode.INVALID_SCHEMA,
                        retryable=False,
                    )
                return None
            return EventValidationError(
                "Global sync identifier is already owned outside the current tenant",
                code=SyncErrorCode.INVALID_SCHEMA,
                retryable=False,
            )

        if constraint_name not in _GLOBAL_ID_CONSTRAINTS:
            return None
        return EventValidationError(
            "Global sync identifier is already owned outside the current tenant",
            code=SyncErrorCode.INVALID_SCHEMA,
            retryable=False,
        )

    def _insert_learning_event_row(
        self,
        connection,
        envelope: SyncEventEnvelope,
        received_at: str,
    ) -> None:
        owner = self.events.learning_event_owner(envelope.event_id)
        if owner is not None and owner != envelope.student_id:
            raise EventValidationError(
                f"Learning event {envelope.event_id} belongs to another student",
                code=SyncErrorCode.INVALID_SCHEMA,
                retryable=False,
            )
        connection.execute(
            """
            INSERT INTO learning_events (
                tenant_id, event_id, student_id, session_id, event_type, payload_json,
                policy_version, content_version, occurred_at, received_at,
                device_id, device_sequence, origin, integrity_hash
            ) VALUES (
                current_setting('app.tenant_id'), %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, 'offline', %s
            )
            """,
            self._event_row(envelope, received_at)[:11] + (envelope.integrity_hash,),
        )

    # ------------------------------------------------------------------
    # Snapshot
    # ------------------------------------------------------------------

    def build_snapshot(
        self,
        student_id: str,
        *,
        in_transaction: bool = False,
    ) -> SnapshotResponse:
        with _transaction_scope(
            self.connection,
            in_transaction=in_transaction,
        ) as connection:
            student_row = connection.execute(
                """
                SELECT *
                FROM students
                WHERE id = %s
                  AND tenant_id = current_setting('app.tenant_id', true)
                """,
                (student_id,),
            ).fetchone()
            if student_row is None:
                raise KeyError(f"Unknown student {student_id}")
            skill_rows = connection.execute(
                """
                SELECT *
                FROM student_skill_states
                WHERE student_id = %s
                  AND tenant_id = current_setting('app.tenant_id', true)
                """,
                (student_id,),
            ).fetchall()
            session_row = connection.execute(
                """
                SELECT *
                FROM study_sessions
                WHERE student_id = %s
                  AND tenant_id = current_setting('app.tenant_id', true)
                ORDER BY updated_at DESC LIMIT 1
                """,
                (student_id,),
            ).fetchone()
            plan_row = connection.execute(
                """
                SELECT *
                FROM study_plans
                WHERE student_id = %s
                  AND tenant_id = current_setting('app.tenant_id', true)
                ORDER BY created_at DESC LIMIT 1
                """,
                (student_id,),
            ).fetchone()
            event_count = connection.execute(
                """
                SELECT COUNT(*) AS total
                FROM learning_events
                WHERE student_id = %s
                  AND tenant_id = current_setting('app.tenant_id', true)
                """,
                (student_id,),
            ).fetchone()["total"]
            latest = connection.execute(
                """
                SELECT COUNT(*) AS total
                FROM learning_events
                WHERE tenant_id = current_setting('app.tenant_id', true)
                """
            ).fetchone()["total"]
            strategy_memory = {
                "intervention_stats": self._intervention_stats(
                    student_id,
                    in_transaction=True,
                ),
                "facts": self._facts_summary(student_id),
            }

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

    def _intervention_stats(
        self,
        student_id: str,
        *,
        in_transaction: bool = False,
    ) -> list[dict]:
        with _transaction_scope(
            self.connection,
            in_transaction=in_transaction,
        ) as connection:
            rows = connection.execute(
                """
                SELECT skill, misconception, intervention, difficulty_band,
                       immediate_correct, immediate_attempts, immediate_weight,
                       short_term_correct, short_term_attempts, short_term_weight,
                       delayed_correct, delayed_attempts, delayed_weight
                FROM intervention_stats
                WHERE student_id = %s
                  AND tenant_id = current_setting('app.tenant_id', true)
                """,
                (student_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def _facts_summary(self, student_id: str) -> list[dict]:
        facts = self.memory.get_facts(student_id)
        return [
            {
                "fact_id": fact.fact_id,
                "student_id": fact.student_id,
                "category": fact.category,
                "normalized_key": fact.normalized_key,
                "fact_text": fact.fact_text,
                "confidence": fact.confidence,
                "supporting_episode_ids": fact.supporting_episode_ids,
                "contradicting_episode_ids": fact.contradicting_episode_ids,
                "evidence_count": fact.evidence_count,
                "contradiction_count": fact.contradiction_count,
                "status": fact.status,
                "version": fact.version,
            }
            for fact in facts[:20]
        ]


def event_type_value(envelope: SyncEventEnvelope) -> str:
    return envelope.event_type


def conflict_detail(envelope: SyncEventEnvelope, question_id: str) -> str:
    return f"parallel attempt on {question_id} by device {envelope.device_id}"
