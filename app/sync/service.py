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
import logging
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Callable

import psycopg
from psycopg.errors import UniqueViolation

from app.agent.hybrid import (
    AuthoritativeEvidence,
    ContentRecord,
    HybridTask,
    MIN_INTERVENTION_STAT_ATTEMPTS,
    ShadowMaterial,
    run_shadow_decision,
    run_shadow_explanation,
    task_enabled,
)
from app.agent.hybrid_contracts import (
    ContentCandidate,
    ExplanationContext,
    ExplanationFact,
    ExplanationProposal,
    HybridDecisionContext,
    HybridShadowObservation,
    InterventionEvidence,
    RecalledEpisodeEvidence,
)
from app.agent.policy import (
    POLICY_VERSION,
    PolicyInput,
    decide_next_action,
    derive_policy_constraints,
)
from app.domain.events import AgentEvent
from app.domain.learner import SkillState
from app.domain.memory import BoundedAction, Episode, InterventionStat
from app.domain.sessions import SessionState
from app.infrastructure.event_store import EventStore
from app.infrastructure.learner_store import LearnerStore
from app.infrastructure.pg import transaction
from app.memory.episode_builder import EpisodeBuilder
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

logger = logging.getLogger(__name__)


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
    def __init__(
        self,
        connection: psycopg.Connection,
        *,
        llm_client: "LLMClient | None" = None,
    ) -> None:
        self.connection = connection
        self.events = EventStore(connection)
        self.learner = LearnerStore(connection)
        self.memory = PGMemory(connection)
        self.episodes = EpisodeBuilder(connection)
        self.answer_keys = VersionedAnswerKey()
        self._llm_client = llm_client
        self.on_shadow_observation: Callable[[HybridShadowObservation], None] | None = None

    def _shadow_client(self) -> "LLMClient":
        """Lazy injectable transport; default constructed from env on first use
        so tests without a key never construct an HTTP transport."""
        if self._llm_client is None:
            from app.agent.llm_client import LLMClient

            self._llm_client = LLMClient()
        return self._llm_client

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
        shadow_sink: list[ShadowMaterial] | None = (
            []
            if task_enabled(HybridTask.DECISION_REASONING)
            or task_enabled(HybridTask.EXPLANATION)
            else None
        )
        with student_advisory_lock(self.connection, request.student_id):
            try:
                with transaction(self.connection):
                    response = self._process_batch_locked(
                        request,
                        in_transaction=True,
                        shadow_sink=shadow_sink,
                    )
            except StudentInactiveError:
                return self._unauthorized_student_response()
        # H4/H5: the deterministic answer/AgentEvent committed; the advisory
        # lock and transaction are released. Shadow work is response-only and
        # may never change the executed action; a verified explanation may
        # only add an optional sentence to the existing explanation surface.
        if shadow_sink:
            explanations = self._run_shadow_observations(shadow_sink)
            for event in response.server_events:
                proposal = explanations.get(event["source_event_id"])
                if proposal is not None:
                    event["personalized_explanation"] = proposal.student_explanation
                    event["personalized_emphasis"] = proposal.emphasis
        return response

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
        shadow_sink: list[ShadowMaterial] | None = None,
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

        ordered_events = sorted(
            request.events,
            key=lambda event: (event.device_sequence, event.event_id),
        )
        for event_index, envelope in enumerate(ordered_events):
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
            if accepted_max_sequence and envelope.device_sequence <= accepted_max_sequence:
                rejected.append(
                    SyncRejectedEvent(
                        event_id=envelope.event_id,
                        code=SyncErrorCode.INVALID_SCHEMA.value,
                        retryable=False,
                    )
                )
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
                            shadow_sink=shadow_sink,
                            in_transaction=True,
                        )
                else:
                    self._apply_event(
                        envelope,
                        accepted,
                        rejected,
                        conflicts,
                        server_agent_events,
                        shadow_sink=shadow_sink,
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
        shadow_sink: list[ShadowMaterial] | None = None,
    ) -> None:
        """Projection-only application; `insert_event_row=False` replays
        already-stored events (used by scripts/rebuild_learner_projections.py)."""
        event_type = envelope.event_type
        if event_type == "ANSWER_SUBMITTED":
            self._apply_answer_submitted(envelope, accepted, rejected, conflicts,
                                         server_agent_events,
                                         insert_event_row=insert_event_row,
                                         in_transaction=in_transaction,
                                         shadow_sink=shadow_sink)
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
            if insert_event_row and event_type_value(envelope) == "WORKED_EXAMPLE_PRESENTED":
                self._start_presented_intervention(envelope)
        accepted.append(envelope.event_id)

    def _start_presented_intervention(self, envelope: SyncEventEnvelope) -> Episode:
        payload = envelope.payload
        required = (
            "source_answer_event_id",
            "content_id",
            "content_version",
            "skill",
            "misconception",
            "intervention",
        )
        if any(not payload.get(field) for field in required):
            raise EventValidationError(
                "worked-example presentation is missing required evidence",
                code=SyncErrorCode.INVALID_SCHEMA,
                retryable=False,
            )
        row = self.connection.execute(
            """
            SELECT ae.action, ae.action_payload_json,
                   le.payload_json AS source_payload_json
            FROM agent_events AS ae
            JOIN learning_events AS le
              ON le.event_id = ae.source_event_id
             AND le.student_id = ae.student_id
             AND le.tenant_id = ae.tenant_id
            WHERE ae.student_id = %s
              AND ae.tenant_id = current_setting('app.tenant_id', true)
              AND ae.session_id = %s
              AND ae.source_event_id = %s
            """,
            (
                envelope.student_id,
                envelope.session_id,
                payload["source_answer_event_id"],
            ),
        ).fetchone()
        if row is None or row["action"] != BoundedAction.SHOW_WORKED_EXAMPLE.value:
            raise EventValidationError(
                "worked-example presentation has no matching server decision",
                code=SyncErrorCode.INVALID_SCHEMA,
                retryable=False,
            )
        expected = json.loads(row["action_payload_json"] or "{}")
        source_payload = json.loads(row["source_payload_json"] or "{}")
        source_question_id = source_payload.get("question_id")
        if (
            expected.get("content_id") != payload["content_id"]
            or expected.get("content_version") != payload["content_version"]
            or expected.get("skill") != payload["skill"]
            or expected.get("misconception") != payload["misconception"]
            or payload["intervention"] != BoundedAction.SHOW_WORKED_EXAMPLE.value
            or envelope.question_id != payload["content_id"]
            or envelope.question_version != payload["content_version"]
            or not source_question_id
        ):
            raise EventValidationError(
                "worked-example presentation does not match the server decision",
                code=SyncErrorCode.INVALID_SCHEMA,
                retryable=False,
            )
        episode_id = "ep_" + hashlib.sha256(
            (
                payload["source_answer_event_id"]
                + ":"
                + payload["content_id"]
            ).encode("utf-8")
        ).hexdigest()[:12]
        existing = self.episodes.get_episode(episode_id)
        if existing is not None:
            return existing
        return self.episodes.start_runtime_candidate(
            student_id=envelope.student_id,
            session_id=envelope.session_id,
            skill=payload["skill"],
            misconception=payload["misconception"],
            intervention=payload["intervention"],
            teaching_content_id=payload["content_id"],
            trigger_content_id=source_question_id,
            evidence_event_id=envelope.event_id,
            episode_id=episode_id,
            commit=False,
        )

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
        shadow_sink: list[ShadowMaterial] | None = None,
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
        agent_event: AgentEvent | None = None
        completed_episode: Episode | None = None
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
                    skill_state = None
                    if meta["skill"]:
                        skill_state = self._update_skill_state(
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

                    if (
                        insert_event_row
                        and not late_event
                        and meta["skill"]
                        and skill_state is not None
                    ):
                        completed_episode = self.episodes.complete_runtime_candidate(
                            student_id=envelope.student_id,
                            session_id=envelope.session_id,
                            skill=meta["skill"],
                            outcome_event_id=envelope.event_id,
                            outcome_content_id=question_id,
                            outcome_correct=correct,
                            outcome_hint_level=int(envelope.payload.get("hint_level", 0)),
                            commit=False,
                        )
                        if completed_episode is not None:
                            self.memory.record_intervention_outcome(
                                student_id=envelope.student_id,
                                skill=completed_episode.skill,
                                misconception=completed_episode.misconception,
                                intervention=completed_episode.intervention,
                                difficulty_band=f"d{meta.get('difficulty') or 2}",
                                window="immediate",
                                component_score=completed_episode.effectiveness,
                                weight=1.0,
                                commit=False,
                            )
                            if completed_episode.status == "validated":
                                self.memory.upsert_fact_for_episode(
                                    completed_episode,
                                    commit=False,
                                )

                        agent_event = self._decide_and_record_agent_event(
                            envelope=envelope,
                            meta=meta,
                            question_id=question_id,
                            question_version=int(question_version),
                            misconception=misconception,
                            skill_state=skill_state,
                            now=received_at,
                            shadow_sink=shadow_sink,
                        )
        accepted.append(envelope.event_id)
        if agent_event is not None:
            server_agent_events.append(
                {
                    "source_event_id": envelope.event_id,
                    "action": agent_event.action,
                    "action_payload": agent_event.action_payload,
                    "reason_code": agent_event.reason_code,
                    "reason_text": agent_event.reason_text,
                    "policy_version": agent_event.policy_version,
                    "episode_ids": agent_event.episode_ids,
                    "state_after": agent_event.state_after,
                    "question_id": question_id,
                    "correct": correct,
                    "misconception": misconception,
                    "validated_episode_id": (
                        completed_episode.episode_id
                        if completed_episode is not None
                        and completed_episode.status == "validated"
                        else None
                    ),
                }
            )

    def _decide_and_record_agent_event(
        self,
        *,
        envelope: SyncEventEnvelope,
        meta: dict,
        question_id: str,
        question_version: int,
        misconception: str | None,
        skill_state: SkillState,
        now: str,
        shadow_sink: list[ShadowMaterial] | None = None,
    ) -> AgentEvent:
        observations = 0
        distinct_items = 0
        recalled: list[Episode] = []
        if misconception:
            row = self.connection.execute(
                """
                SELECT COUNT(*) AS observations,
                       COUNT(DISTINCT item_id) AS distinct_items
                FROM misconception_evidence
                WHERE student_id = %s
                  AND tenant_id = current_setting('app.tenant_id', true)
                  AND session_id = %s
                  AND skill = %s
                  AND misconception = %s
                """,
                (
                    envelope.student_id,
                    envelope.session_id,
                    meta["skill"],
                    misconception,
                ),
            ).fetchone()
            observations = row["observations"]
            distinct_items = row["distinct_items"]
            recalled = self.memory.recall_episodes(
                student_id=envelope.student_id,
                skill=meta["skill"],
                misconception=misconception,
                limit=3,
            )

        recent = self.connection.execute(
            """
            SELECT content_id, version, correct, hint_level
            FROM answer_attempts
            WHERE student_id = %s
              AND tenant_id = current_setting('app.tenant_id', true)
              AND session_id = %s
            ORDER BY occurred_at DESC
            LIMIT 20
            """,
            (envelope.student_id, envelope.session_id),
        ).fetchall()
        skill_recent = []
        pack_key = self.answer_keys.pack(envelope.content_pack_version)
        for attempt in recent:
            try:
                attempt_meta = pack_key.item_meta(
                    attempt["content_id"],
                    int(attempt["version"]),
                )
            except QuestionVersionError:
                continue
            if attempt_meta.get("skill") == meta["skill"]:
                skill_recent.append(attempt)
        consecutive_errors = 0
        for attempt in skill_recent:
            if attempt["correct"]:
                break
            consecutive_errors += 1
        correct_streak = 0
        for attempt in skill_recent:
            if not attempt["correct"]:
                break
            correct_streak += 1
        inputs = PolicyInput(
            student_id=envelope.student_id,
            session_id=envelope.session_id,
            skill=meta["skill"],
            subskill=meta.get("subskill"),
            difficulty=meta.get("difficulty") or 2,
            mastery=skill_state.mastery,
            confidence=skill_state.confidence,
            consecutive_errors=consecutive_errors,
            correct_streak=correct_streak,
            repeated_misconception=observations >= 2 and distinct_items >= 2,
            active_misconception=misconception,
            misconception_observation_count=observations,
            misconception_distinct_items=distinct_items,
            minutes_remaining=int(envelope.payload.get("minutes_remaining", 20)),
            hints_used_this_item=int(envelope.payload.get("hint_level", 0)),
            recalled_successful_episode=bool(recalled),
            recalled_episode_ids=[episode.episode_id for episode in recalled],
            recent_correct_without_high_hint=sum(
                1
                for row in skill_recent[:3]
                if row["correct"] and row["hint_level"] <= 1
            ),
            recent_total=min(3, len(skill_recent)),
        )
        result = decide_next_action(inputs)
        decision = result.decision
        referenced_content = [question_id]
        lesson_type = {
            BoundedAction.SHOW_WORKED_EXAMPLE.value: "worked_example",
            BoundedAction.SHOW_MICRO_LESSON.value: "micro_lesson",
        }.get(decision.action)
        if lesson_type:
            lesson = pack_key.teaching_asset_meta(
                meta["skill"], lesson_type, misconception
            )
            if lesson is not None:
                decision = decision.model_copy(
                    update={
                        "action_payload": {
                            **decision.action_payload,
                            "content_id": lesson["id"],
                            "content_version": lesson["version"],
                            "review_status": lesson["review_status"],
                            "license": lesson["license"],
                            "source_lineage": lesson["source_lineage"],
                        },
                        "content_id": lesson["id"],
                    }
                )
                referenced_content.append(lesson["id"])
        event_id = "agt_" + hashlib.sha256(envelope.event_id.encode("utf-8")).hexdigest()[:16]
        agent_event = AgentEvent(
            event_id=event_id,
            student_id=envelope.student_id,
            session_id=envelope.session_id,
            source_event_id=envelope.event_id,
            state_before=SessionState.ANSWER_EVALUATED.value,
            state_after=result.next_state.value,
            action=decision.action,
            action_payload=decision.action_payload,
            reason_code=decision.reason_code,
            reason_text=decision.reason_text,
            policy_version=decision.policy_version,
            content_version=f"{question_id}.v{question_version}",
            referenced_content=referenced_content,
            episode_ids=decision.episode_ids,
            source="offline",
            created_at=now,
        )
        self.events.append_agent_event(
            agent_event,
            on_duplicate="raise",
            commit=False,
        )
        self._transition_session(
            self.connection,
            envelope.student_id,
            envelope.session_id,
            result.next_state,
            now,
        )
        if shadow_sink is not None:
            shadow_sink.append(
                self._build_shadow_material(
                    envelope=envelope,
                    meta=meta,
                    inputs=inputs,
                    constraints=derive_policy_constraints(inputs),
                    decision=decision,
                    recalled=recalled,
                    skill_state=skill_state,
                    pack_key=pack_key,
                    now=now,
                )
            )
        return agent_event

    # ------------------------------------------------------------------
    # Hybrid shadow material (H4): sanitized, scoped context captured while
    # the authoritative transaction is active, consumed post-commit.
    # ------------------------------------------------------------------

    @staticmethod
    def _recency_bucket(created_at: str, now: str) -> str:
        try:
            created = datetime.fromisoformat(created_at)
            reference = datetime.fromisoformat(now)
        except ValueError:
            return "older"
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        if reference.tzinfo is None:
            reference = reference.replace(tzinfo=UTC)
        if reference - created <= timedelta(days=7):
            return "recent"
        if reference - created <= timedelta(days=30):
            return "medium"
        return "older"

    def _shadow_episode_evidence(
        self, episode: Episode, now: str
    ) -> RecalledEpisodeEvidence:
        outcome = episode.outcome or {}
        intervention = BoundedAction(episode.intervention)
        return RecalledEpisodeEvidence(
            episode_id=episode.episode_id,
            skill=episode.skill,
            misconception=episode.misconception,
            intervention=intervention,
            outcome_correct=bool(outcome.get("correct")),
            different_item=bool(outcome.get("different_item")),
            effectiveness=episode.effectiveness,
            confidence=episode.confidence,
            status="validated",
            recency_bucket=self._recency_bucket(episode.created_at, now),
            teaching_content_id=outcome.get("teaching_content_id"),
            difficulty_band=None,
        )

    def _shadow_content_candidates(
        self, pack_key, skill: str, misconception: str | None
    ) -> tuple[list[ContentCandidate], dict[str, ContentRecord]]:
        candidates: list[ContentCandidate] = []
        records: dict[str, ContentRecord] = {}
        for content_type in ("worked_example", "micro_lesson"):
            lesson = pack_key.teaching_asset_meta(skill, content_type, misconception)
            if lesson is None or not lesson.get("content_hash"):
                continue
            candidates.append(
                ContentCandidate(
                    content_id=lesson["id"],
                    content_type=lesson["content_type"],
                    skill=skill,
                    misconceptions=tuple(lesson["target_misconceptions"]),
                    pack_version=lesson["pack_version"],
                    content_hash=lesson["content_hash"],
                    review_status="approved",
                    human_approved=False,
                )
            )
            records[lesson["id"]] = ContentRecord(
                content_id=lesson["id"],
                content_hash=lesson["content_hash"],
                review_status=lesson["review_status"],
                content_type=lesson["content_type"],
                target_skill=lesson["target_skill"] or skill,
                misconceptions=tuple(lesson["target_misconceptions"]),
                license_id=lesson.get("license_id") or "",
                license_name=lesson.get("license_name") or "",
                source_id=lesson.get("source_id") or "",
                pack_version=lesson["pack_version"],
                human_approved=False,
                body="",
            )
        return candidates, records

    def _shadow_intervention_evidence(
        self, student_id: str, skill: str
    ) -> list[InterventionEvidence]:
        entries: list[InterventionEvidence] = []
        for row in self._intervention_stats(student_id, in_transaction=True):
            if row["skill"] != skill:
                continue
            try:
                intervention = BoundedAction(row["intervention"])
            except ValueError:
                continue
            stat = InterventionStat(
                stat_id=f"shadow_{row['intervention']}_{row['difficulty_band']}",
                student_id="",
                skill=row["skill"],
                misconception=row["misconception"],
                intervention=row["intervention"],
                difficulty_band=row["difficulty_band"],
                immediate_correct=row["immediate_correct"],
                immediate_attempts=row["immediate_attempts"],
                immediate_weight=row["immediate_weight"],
                short_term_correct=row["short_term_correct"],
                short_term_attempts=row["short_term_attempts"],
                short_term_weight=row["short_term_weight"],
                delayed_correct=row["delayed_correct"],
                delayed_attempts=row["delayed_attempts"],
                delayed_weight=row["delayed_weight"],
            )
            entries.append(
                InterventionEvidence(
                    intervention=intervention,
                    difficulty_band=row["difficulty_band"],
                    immediate_attempts=row["immediate_attempts"],
                    short_term_attempts=row["short_term_attempts"],
                    delayed_attempts=row["delayed_attempts"],
                    blended_effectiveness=stat.blended_effectiveness(),
                    support=(
                        "supported"
                        if row["immediate_attempts"] >= MIN_INTERVENTION_STAT_ATTEMPTS
                        else "insufficient"
                    ),
                )
            )
            if len(entries) == 8:
                break
        return entries

    def _build_shadow_material(
        self,
        *,
        envelope: SyncEventEnvelope,
        meta: dict,
        inputs: PolicyInput,
        constraints,
        decision,
        recalled: list[Episode],
        skill_state: SkillState,
        pack_key,
        now: str,
    ) -> ShadowMaterial:
        """Bounded shadow context and scoped evidence for post-commit work."""
        episodes = [self._shadow_episode_evidence(episode, now) for episode in recalled]
        candidates, records = self._shadow_content_candidates(
            pack_key, meta["skill"], inputs.active_misconception
        )
        evidence_counts = inputs.misconception_observation_count
        confidence_label = (
            "high" if evidence_counts >= 2 else "medium" if evidence_counts >= 1 else "low"
        )
        context = HybridDecisionContext.model_validate(
            dict(
                task="intervention_ranking",
                skill=meta["skill"],
                subskill=meta.get("subskill"),
                difficulty=meta.get("difficulty") or 2,
                mastery=skill_state.mastery,
                mastery_confidence=skill_state.confidence,
                consecutive_errors=inputs.consecutive_errors,
                correct_streak=inputs.correct_streak,
                active_misconception=inputs.active_misconception,
                misconception_evidence_count=evidence_counts,
                misconception_confidence=confidence_label,
                hints_used=inputs.hints_used_this_item,
                minutes_remaining=inputs.minutes_remaining,
                current_state=SessionState.ANSWER_EVALUATED,
                allowed_actions=constraints.allowed_actions,
                deterministic_fallback=decision,
                recalled_episodes=episodes,
                intervention_stats=self._shadow_intervention_evidence(
                    envelope.student_id, meta["skill"]
                ),
                content_candidates=candidates,
            )
        )
        evidence = AuthoritativeEvidence(
            episodes={episode.episode_id: episode for episode in recalled},
            content=records,
            expected_student_id=envelope.student_id,
        )
        return ShadowMaterial(
            source_event_id=envelope.event_id,
            context=context,
            constraints=constraints,
            evidence=evidence,
            fallback=decision,
            explanation=self._build_explanation_context(
                inputs=inputs,
                decision=decision,
                recalled=recalled,
                pack_key=pack_key,
            ),
        )

    @staticmethod
    def _build_explanation_context(
        *,
        inputs: PolicyInput,
        decision,
        recalled: list[Episode],
        pack_key,
    ) -> ExplanationContext | None:
        """Sanitized H5 context for the executed teaching action (plan
        Section 14): only when the action shows a lesson, with grounded
        facts as the single source of numbers and claims."""
        lesson_type = {
            BoundedAction.SHOW_WORKED_EXAMPLE: "worked_example",
            BoundedAction.SHOW_MICRO_LESSON: "micro_lesson",
        }.get(BoundedAction(decision.action))
        if lesson_type is None:
            return None
        lesson = pack_key.teaching_asset_meta(
            inputs.skill, lesson_type, inputs.active_misconception
        )
        lesson_title = lesson.get("title") if lesson is not None else None
        misconception = inputs.active_misconception
        evidence_counts = inputs.misconception_observation_count
        confidence_label = (
            "high" if evidence_counts >= 2 else "medium" if evidence_counts >= 1 else "low"
        )
        facts: list[ExplanationFact] = []
        for episode in recalled[:3]:
            outcome = episode.outcome or {}
            if not (outcome.get("correct") and outcome.get("different_item")):
                continue
            if episode.effectiveness < 0.6 or episode.confidence < 0.5:
                continue
            facts.append(
                ExplanationFact(
                    ref=f"episode:{episode.episode_id}",
                    phrase=(
                        f"A validated {episode.intervention} episode on this "
                        "skill was followed by success on a different item."
                    ),
                )
            )
        if evidence_counts >= 1 and misconception:
            facts.append(
                ExplanationFact(
                    ref="stat:misconception",
                    phrase=(
                        f"{evidence_counts} recorded {misconception} error"
                        f"{'s' if evidence_counts != 1 else ''} in this session"
                    ),
                )
            )
        if inputs.consecutive_errors >= 1:
            facts.append(
                ExplanationFact(
                    ref="stat:consecutive_errors",
                    phrase=(
                        f"{inputs.consecutive_errors} consecutive wrong answer"
                        f"{'s' if inputs.consecutive_errors != 1 else ''} on {inputs.skill}"
                    ),
                )
            )
        facts.append(
            ExplanationFact(
                ref="stat:mastery",
                phrase=f"mastery {inputs.mastery:.2f}",
            )
        )
        protected = [decision.reason_text]
        if lesson_title:
            protected.append(lesson_title)
        return ExplanationContext(
            task="explanation",
            skill=inputs.skill,
            subskill=inputs.subskill,
            fallback_action=BoundedAction(decision.action),
            reason_code=decision.reason_code,
            reason_text=decision.reason_text,
            lesson_title=lesson_title,
            misconception=misconception,
            misconception_evidence_count=evidence_counts,
            misconception_confidence=confidence_label,
            learner_summary=(
                f"{inputs.consecutive_errors} wrong answers in a row on "
                f"{inputs.skill}; {evidence_counts} recorded misconception "
                "errors this session."
            ),
            facts=tuple(facts),
            protected_spans=tuple(span for span in protected if span),
        )

    def _run_shadow_observations(
        self, materials: list[ShadowMaterial]
    ) -> dict[str, ExplanationProposal]:
        """Post-commit shadow gateway: verified decision observations and
        verified personalized explanations. Returns explanations keyed by
        source event id; failures only suppress enrichment, never the
        executed deterministic response."""
        explanations: dict[str, ExplanationProposal] = {}
        for material in materials:
            try:
                observation = run_shadow_decision(material, self._shadow_client())
            except Exception:
                logger.exception(
                    "hybrid shadow failed for source_event_id=%s",
                    material.source_event_id,
                )
                observation = None
            if observation is not None:
                if self.on_shadow_observation is not None:
                    self.on_shadow_observation(observation)
                logger.info(
                    "hybrid_shadow_observation source_event_id=%s fallback=%s "
                    "proposal=%s accepted=%s would_change=%s rejection=%s latency_ms=%d",
                    observation.source_event_id,
                    observation.fallback_action.value,
                    observation.model_proposal_action.value
                    if observation.model_proposal_action is not None
                    else "none",
                    observation.accepted,
                    observation.would_change,
                    observation.rejection_reason or "ok",
                    observation.latency_ms,
                )
            if material.explanation is not None:
                proposal = run_shadow_explanation(
                    material.explanation, self._shadow_client()
                )
                if proposal is not None:
                    explanations[material.source_event_id] = proposal
        return explanations

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
    ) -> SkillState:
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
        return state

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
                "validated_episodes": self._validated_episode_summary(student_id),
                "recent_agent_events": self._recent_agent_events(student_id),
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

    def _validated_episode_summary(self, student_id: str) -> list[dict]:
        rows = self.connection.execute(
            """
            SELECT episode_id, session_id, skill, misconception, intervention,
                   effectiveness, summary, created_at
            FROM learning_episodes
            WHERE student_id = %s
              AND tenant_id = current_setting('app.tenant_id', true)
              AND status = 'validated'
              AND effectiveness >= 0.6
            ORDER BY created_at DESC
            LIMIT 20
            """,
            (student_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def _recent_agent_events(self, student_id: str) -> list[dict]:
        rows = self.connection.execute(
            """
            SELECT source_event_id, session_id, action, action_payload_json,
                   reason_code, reason_text, policy_version, episode_ids_json,
                   state_after, created_at
            FROM agent_events
            WHERE student_id = %s
              AND tenant_id = current_setting('app.tenant_id', true)
            ORDER BY created_at DESC
            LIMIT 10
            """,
            (student_id,),
        ).fetchall()
        return [
            {
                "source_event_id": row["source_event_id"],
                "session_id": row["session_id"],
                "action": row["action"],
                "action_payload": json.loads(row["action_payload_json"] or "{}"),
                "reason_code": row["reason_code"],
                "reason_text": row["reason_text"],
                "policy_version": row["policy_version"],
                "episode_ids": json.loads(row["episode_ids_json"] or "[]"),
                "state_after": row["state_after"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]


def event_type_value(envelope: SyncEventEnvelope) -> str:
    return envelope.event_type


def conflict_detail(envelope: SyncEventEnvelope, question_id: str) -> str:
    return f"parallel attempt on {question_id} by device {envelope.device_id}"
