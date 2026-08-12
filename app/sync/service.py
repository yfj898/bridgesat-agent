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
import time
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
    DecisionToken,
    HybridTask,
    MIN_INTERVENTION_EFFECT_GAP,
    MIN_INTERVENTION_STAT_ATTEMPTS,
    MIN_INTERVENTION_SUPPORT,
    ShadowMaterial,
    run_shadow_decision,
    run_shadow_explanation,
    run_shadow_summary,
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
    SessionSummaryContext,
    SummaryFact,
)
from app.agent.policy import (
    POLICY_VERSION,
    PolicyConstraints,
    PolicyInput,
    PolicyResult,
    decide_next_action,
    derive_policy_constraints,
)
from app.domain.events import AgentDecision, AgentEvent
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
    PersonalizedSummary,
    SyncRejectedEvent,
    SyncRequest,
    SyncResponse,
    SnapshotResponse,
)
from .versioned_scoring import QuestionVersionError, VersionedAnswerKey

logger = logging.getLogger(__name__)

MAX_SUMMARY_FACTS = 16
MAX_SUMMARIES_PER_BATCH = 8
MAX_SUMMARY_BATCH_SECONDS = 5.0


def _bounded_summary_facts(facts: list[SummaryFact]) -> tuple[SummaryFact, ...]:
    unique_facts: list[SummaryFact] = []
    seen_refs: set[str] = set()
    for fact in facts:
        if fact.ref not in seen_refs:
            unique_facts.append(fact)
            seen_refs.add(fact.ref)
    if len(unique_facts) <= MAX_SUMMARY_FACTS:
        return tuple(unique_facts)
    stable_facts = [
        fact
        for fact in unique_facts
        if not fact.ref.startswith("stat:misconception:")
    ]
    misconception_facts = [
        fact
        for fact in unique_facts
        if fact.ref.startswith("stat:misconception:")
    ]
    if len(stable_facts) >= MAX_SUMMARY_FACTS:
        return tuple(stable_facts[:MAX_SUMMARY_FACTS])
    remaining = max(0, MAX_SUMMARY_FACTS - len(stable_facts))
    return tuple(stable_facts + misconception_facts[:remaining])


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
            or task_enabled(HybridTask.ACTION_RANKING)
            else None
        )
        summary_sink: list[tuple[SyncEventEnvelope, SessionSummaryContext | None]] | None = (
            [] if task_enabled(HybridTask.SUMMARY) else None
        )
        with student_advisory_lock(self.connection, request.student_id):
            try:
                with transaction(self.connection):
                    response = self._process_batch_locked(
                        request,
                        in_transaction=True,
                        shadow_sink=shadow_sink,
                        summary_sink=summary_sink,
                    )
            except StudentInactiveError:
                return self._unauthorized_student_response()
        # H4/H5: the deterministic answer/AgentEvent committed; the advisory
        # lock and transaction are released. Shadow work is response-only and
        # may never change the executed action; a verified explanation may
        # only add an optional sentence to the existing explanation surface.
        # H7 (Section 22): only under BRIDGESAT_HYBRID_ACTION_RANKING_ENABLED
        # a verified proposal may replace the response action, and only when
        # the short Phase C revalidation transaction confirms the Phase A
        # decision token (source event, fallback, session boundary) is still
        # current. The durable agent event stays the deterministic fallback;
        # the verified action is served plus an auditable decision trace.
        if shadow_sink:
            explanations, observations = self._run_shadow_observations(shadow_sink)
            if task_enabled(HybridTask.ACTION_RANKING):
                self._apply_ranked_actions(response, shadow_sink, observations)
            for event in response.server_events:
                proposal = explanations.get(event["source_event_id"])
                if proposal is not None and not event.get("hybrid_ranked"):
                    event["personalized_explanation"] = proposal.student_explanation
                    event["personalized_emphasis"] = proposal.emphasis
        # H8 (Section 15): fact assembly happened inside the authoritative
        # completion transaction. Only the provider call and response
        # serialization happen here, after the lock is released.
        if summary_sink:
            summaries: list[PersonalizedSummary] = []
            summary_deadline = time.monotonic() + MAX_SUMMARY_BATCH_SECONDS
            for envelope, context in summary_sink:
                if time.monotonic() >= summary_deadline:
                    logger.warning(
                        "hybrid_summary_batch_budget_exhausted summaries=%s",
                        len(summaries),
                    )
                    break
                if context is None:
                    continue
                sync_fact = SummaryFact(
                    ref="stat:sync",
                    phrase="session completion was accepted",
                )
                context = context.model_copy(
                    update={
                        "session_summary_facts": _bounded_summary_facts(
                            [*context.session_summary_facts, sync_fact]
                        )
                    }
                )
                try:
                    remaining_ms = max(
                        1,
                        int((summary_deadline - time.monotonic()) * 1000),
                    )
                    proposal = run_shadow_summary(
                        context,
                        self._shadow_client(),
                        timeout_ms=remaining_ms,
                    )
                except Exception:
                    logger.warning(
                        "hybrid_summary_failed source_event_id=%s session_id=%s",
                        envelope.event_id,
                        envelope.session_id,
                        exc_info=True,
                    )
                    continue
                if proposal is not None:
                    try:
                        summaries.append(
                            PersonalizedSummary(
                                source_event_id=envelope.event_id,
                                session_id=envelope.session_id,
                                summary_text=proposal.summary_text,
                            )
                        )
                    except Exception:
                        logger.warning(
                            "hybrid_summary_response_failed source_event_id=%s session_id=%s",
                            envelope.event_id,
                            envelope.session_id,
                            exc_info=True,
                        )
            response.personalized_summaries = summaries
            if len(summaries) == 1:
                response.personalized_summary = summaries[0].summary_text
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
        summary_sink: list[tuple[SyncEventEnvelope, SessionSummaryContext | None]] | None = None,
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
                        detail=str(exc),
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
                            summary_sink=summary_sink,
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
                        summary_sink=summary_sink,
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
                        detail=str(exc),
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
                    detail=f"Dependency {dep} is not stored",
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
        summary_sink: list[tuple[SyncEventEnvelope, SessionSummaryContext | None]] | None = None,
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
                                          in_transaction=in_transaction,
                                          summary_sink=summary_sink)
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
            if insert_event_row and event_type_value(envelope) in (
                "WORKED_EXAMPLE_PRESENTED",
                "MICRO_LESSON_PRESENTED",
            ):
                self._start_presented_intervention(envelope)
        accepted.append(envelope.event_id)

    def _start_presented_intervention(self, envelope: SyncEventEnvelope) -> Episode:
        payload = envelope.payload
        expected_action_by_event = {
            "WORKED_EXAMPLE_PRESENTED": BoundedAction.SHOW_WORKED_EXAMPLE.value,
            "MICRO_LESSON_PRESENTED": BoundedAction.SHOW_MICRO_LESSON.value,
        }
        expected_action = expected_action_by_event.get(event_type_value(envelope))
        required = (
            "source_answer_event_id",
            "content_id",
            "content_version",
            "skill",
            "intervention",
        )
        misconception = payload.get("misconception")
        if (
            expected_action is None
            or any(not payload.get(field) for field in required)
            or payload.get("intervention") != expected_action
            or (
                misconception is not None
                and (not isinstance(misconception, str) or not misconception)
            )
        ):
            raise EventValidationError(
                "teaching intervention presentation is missing required evidence",
                code=SyncErrorCode.INVALID_SCHEMA,
                retryable=False,
            )
        row = self.connection.execute(
            """
            SELECT ae.action, ae.action_payload_json,
                   le.payload_json AS source_payload_json,
                   hdt.verified_action,
                   hdt.decision_token
            FROM agent_events AS ae
            JOIN learning_events AS le
              ON le.event_id = ae.source_event_id
             AND le.student_id = ae.student_id
             AND le.tenant_id = ae.tenant_id
            LEFT JOIN hybrid_decision_trace AS hdt
              ON hdt.source_event_id = ae.source_event_id
             AND hdt.student_id = ae.student_id
             AND hdt.tenant_id = ae.tenant_id
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
        if row is None:
            raise EventValidationError(
                "teaching intervention presentation has no matching server decision",
                code=SyncErrorCode.INVALID_SCHEMA,
                retryable=False,
            )
        if envelope.content_pack_version != self.answer_keys.pack(
            envelope.content_pack_version
        ).pack_version:
            raise EventValidationError(
                "teaching intervention presentation references an unknown pack",
                code=SyncErrorCode.QUESTION_VERSION_UNKNOWN,
                retryable=False,
            )
        expected = json.loads(row["action_payload_json"] or "{}")
        source_payload = json.loads(row["source_payload_json"] or "{}")
        source_question_id = source_payload.get("question_id")
        durable_action = row["action"]
        served_action = row["verified_action"] or durable_action
        if served_action != expected_action:
            raise EventValidationError(
                "teaching intervention presentation has no matching served action: "
                f"durable={durable_action!r} verified={row['verified_action']!r} "
                f"expected={expected_action!r}",
                code=SyncErrorCode.INVALID_SCHEMA,
                retryable=False,
            )
        lesson_type = {
            BoundedAction.SHOW_WORKED_EXAMPLE.value: "worked_example",
            BoundedAction.SHOW_MICRO_LESSON.value: "micro_lesson",
        }.get(expected_action)
        if lesson_type is None:
            raise EventValidationError(
                "teaching intervention presentation has invalid action",
                code=SyncErrorCode.INVALID_SCHEMA,
                retryable=False,
            )
        if row["verified_action"]:
            try:
                trace_token = json.loads(row["decision_token"] or "{}")
            except (TypeError, json.JSONDecodeError):
                trace_token = None
            verified_payload = (
                trace_token.get("verified_action_payload")
                if isinstance(trace_token, dict)
                else None
            )
            if not isinstance(verified_payload, dict) or not self._lesson_matches_verified_payload(
                pack_version=envelope.content_pack_version,
                lesson_type=lesson_type,
                misconception=misconception,
                payload=verified_payload,
            ):
                raise EventValidationError(
                    "ranked intervention presentation does not match verified content",
                    code=SyncErrorCode.INVALID_SCHEMA,
                    retryable=False,
                )
            expected = verified_payload
        elif not self._lesson_matches_durable_payload(
            pack_version=envelope.content_pack_version,
            lesson_type=lesson_type,
            misconception=misconception,
            payload=expected,
        ):
            raise EventValidationError(
                "teaching intervention presentation does not match approved content",
                code=SyncErrorCode.INVALID_SCHEMA,
                retryable=False,
            )
        if (
            expected.get("content_id") != payload["content_id"]
            or str(expected.get("content_version"))
            != str(payload["content_version"])
            or expected.get("skill") != payload["skill"]
            or expected.get("misconception") != misconception
            or expected.get("review_status") != "approved"
            or envelope.question_id != payload["content_id"]
            or envelope.question_version != payload["content_version"]
            or not source_question_id
        ):
            raise EventValidationError(
                "teaching intervention presentation does not match the server decision",
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
            misconception=misconception,
            intervention=payload["intervention"],
            teaching_content_id=payload["content_id"],
            trigger_content_id=source_question_id,
            evidence_event_id=envelope.event_id,
            episode_id=episode_id,
            commit=False,
        )

    def _lesson_matches_durable_payload(
        self,
        *,
        pack_version: str,
        lesson_type: str,
        misconception: str | None,
        payload: dict,
    ) -> bool:
        try:
            lesson = self.answer_keys.pack(pack_version).teaching_asset_meta(
                payload.get("skill"), lesson_type, misconception
            )
        except QuestionVersionError:
            return False
        return bool(
            lesson is not None
            and lesson["id"] == payload.get("content_id")
            and str(lesson["version"]) == str(payload.get("content_version"))
            and lesson["review_status"] == "approved"
            and self._registry_matches_lesson(lesson=lesson)
            and payload.get("skill") == lesson["target_skill"]
            and payload.get("misconception") == misconception
            and (
                misconception is None
                or not lesson["target_misconceptions"]
                or misconception in lesson["target_misconceptions"]
            )
        )

    def _lesson_matches_verified_payload(
        self,
        *,
        pack_version: str,
        lesson_type: str,
        misconception: str | None,
        payload: dict,
    ) -> bool:
        if not self._lesson_matches_durable_payload(
            pack_version=pack_version,
            lesson_type=lesson_type,
            misconception=misconception,
            payload=payload,
        ):
            return False
        try:
            lesson = self.answer_keys.pack(pack_version).teaching_asset_meta(
                payload.get("skill"), lesson_type, misconception
            )
        except QuestionVersionError:
            return False
        return bool(
            lesson is not None
            and payload.get("content_hash") == lesson["content_hash"]
            and payload.get("pack_version") == lesson["pack_version"]
            and payload.get("source_lineage") == lesson["source_lineage"]
            and payload.get("review_status") == "approved"
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
        summary_sink: list[tuple[SyncEventEnvelope, SessionSummaryContext | None]] | None = None,
    ) -> None:
        received_at = _utc_now_iso()
        summarize = False
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
                summarize = True
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
                summarize = True
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
            if (
                summary_sink is not None
                and summarize
                and len(summary_sink) < MAX_SUMMARIES_PER_BATCH
            ):
                context = None
                try:
                    with _event_savepoint(self.connection, "summary_context"):
                        context = self._build_session_summary_context(envelope)
                except Exception:
                    logger.warning(
                        "hybrid_summary_context_failed source_event_id=%s session_id=%s",
                        envelope.event_id,
                        envelope.session_id,
                        exc_info=True,
                    )
                if context is not None:
                    summary_sink.append((envelope, context))
        accepted.append(envelope.event_id)

    def _build_session_summary_context(
        self, envelope: SyncEventEnvelope
    ) -> SessionSummaryContext | None:
        """Deterministic H8 fact allowlist for one completed session, derived
        from the committed session state (plan Section 15). Every fact phrase
        carries its own numbers, so numeric grounding checks against them.
        Returns None for an empty session (nothing to summarize)."""
        facts: list[SummaryFact] = []
        attempts = self.connection.execute(
            """
            SELECT COUNT(*) AS n FROM answer_attempts
            WHERE student_id = %s AND session_id = %s
              AND tenant_id = current_setting('app.tenant_id', true)
              AND validity = 'valid'
            """,
            (envelope.student_id, envelope.session_id),
        ).fetchone()["n"]
        if attempts:
            facts.append(
                SummaryFact(
                    ref="stat:attempts",
                    phrase=(
                        f"{attempts} question{'s' if attempts != 1 else ''} "
                        "attempted in this session"
                    ),
                )
            )
        skills = self.connection.execute(
            """
            SELECT COUNT(DISTINCT ci.target_skill) AS n
            FROM answer_attempts aa
            JOIN content_items ci
              ON ci.content_id = aa.content_id AND ci.version = aa.version
            WHERE aa.student_id = %s AND aa.session_id = %s
              AND aa.tenant_id = current_setting('app.tenant_id', true)
              AND aa.validity = 'valid'
            """,
            (envelope.student_id, envelope.session_id),
        ).fetchone()["n"]
        if skills:
            facts.append(
                SummaryFact(
                    ref="stat:skills",
                    phrase=(
                        f"{skills} skill{'s' if skills != 1 else ''} practiced "
                        "this session"
                    ),
                )
            )
        misconception_rows = self.connection.execute(
            """
            SELECT skill, misconception, confidence_label, COUNT(*) AS n
            FROM misconception_evidence
            WHERE student_id = %s AND session_id = %s
              AND tenant_id = current_setting('app.tenant_id', true)
            GROUP BY skill, misconception, confidence_label
            ORDER BY skill, misconception
            """,
            (envelope.student_id, envelope.session_id),
        ).fetchall()
        for row in misconception_rows:
            facts.append(
                SummaryFact(
                    ref=(
                        f"stat:misconception:{row['skill']}:{row['misconception']}"
                        f":{row['confidence_label']}"
                    ),
                    phrase=(
                        f"{row['misconception']} evidence recorded "
                        f"{row['n']} time{'s' if row['n'] != 1 else ''} on "
                        f"{row['skill']} ({row['confidence_label']} confidence)"
                    ),
                )
            )
        intervention_rows = self.connection.execute(
            """
            SELECT DISTINCT event_type FROM learning_events
            WHERE student_id = %s AND session_id = %s
              AND event_type IN ('WORKED_EXAMPLE_PRESENTED', 'MICRO_LESSON_PRESENTED')
              AND tenant_id = current_setting('app.tenant_id', true)
            ORDER BY event_type
            """,
            (envelope.student_id, envelope.session_id),
        ).fetchall()
        if intervention_rows:
            labels = {
                "WORKED_EXAMPLE_PRESENTED": "worked example",
                "MICRO_LESSON_PRESENTED": "micro lesson",
            }
            shown = " and ".join(
                labels[row["event_type"]] for row in intervention_rows
            )
            facts.append(
                SummaryFact(
                    ref="stat:interventions",
                    phrase=f"{shown} presentation was confirmed this session",
                )
            )
        episodes = self.connection.execute(
            """
            SELECT COUNT(*) AS n FROM learning_episodes
            WHERE student_id = %s AND session_id = %s AND status = 'validated'
              AND tenant_id = current_setting('app.tenant_id', true)
            """,
            (envelope.student_id, envelope.session_id),
        ).fetchone()["n"]
        if episodes:
            facts.append(
                SummaryFact(
                    ref="stat:episodes",
                    phrase=(
                        f"{episodes} validated learning strateg"
                        f"{'y' if episodes == 1 else 'ies'} recorded this session"
                    ),
                )
            )
        transfer = self.connection.execute(
            """
            SELECT COUNT(*) AS n FROM learning_episodes
            WHERE student_id = %s AND session_id = %s AND status = 'validated'
              AND outcome_json::jsonb ->> 'different_item' = 'true'
              AND outcome_json::jsonb ->> 'correct' = 'true'
              AND tenant_id = current_setting('app.tenant_id', true)
            """,
            (envelope.student_id, envelope.session_id),
        ).fetchone()["n"]
        if transfer:
            facts.append(
                SummaryFact(
                    ref="stat:transfer",
                    phrase=(
                        f"{transfer} transfer success{'es' if transfer != 1 else ''} "
                        "on a different item recorded"
                    ),
                )
            )
        due_review = self.connection.execute(
            """
            SELECT DISTINCT s.skill FROM student_skill_states s
            JOIN answer_attempts aa
              ON aa.student_id = s.student_id AND aa.session_id = %s
            JOIN content_items ci
              ON ci.content_id = aa.content_id
             AND ci.version = aa.version
             AND ci.target_skill = s.skill
            WHERE aa.tenant_id = current_setting('app.tenant_id', true)
              AND s.student_id = %s
              AND s.review_due_at IS NOT NULL
              AND s.review_due_at <= %s
              AND s.evidence_count > 0
              AND s.tenant_id = current_setting('app.tenant_id', true)
            ORDER BY s.skill
            """,
            (envelope.session_id, envelope.student_id, _utc_now_iso()),
        ).fetchall()
        for row in due_review:
            facts.append(
                SummaryFact(
                    ref=f"stat:review:{row['skill']}",
                    phrase=f"{row['skill']} is due for review next session",
                )
            )
        unique_facts = _bounded_summary_facts(facts)
        if not unique_facts:
            return None
        return SessionSummaryContext.model_validate(
            dict(task="session_summary", session_summary_facts=tuple(unique_facts))
        )

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
            raise EventValidationError(
                "answer submission is missing question or selected choice",
                code=SyncErrorCode.INVALID_SCHEMA,
                retryable=False,
            )

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
        except QuestionVersionError as exc:
            rejected.append(
                SyncRejectedEvent(
                    event_id=envelope.event_id,
                    code=SyncErrorCode.QUESTION_VERSION_UNKNOWN.value,
                    retryable=False,
                    detail=str(exc),
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
        contradicted: list[Episode] = []
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
            contradicted = self.memory.recall_contradicting_episodes(
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
        recalled_ids_by_intervention: dict[str, list[str]] = {}
        for episode in recalled:
            if episode.intervention not in {
                BoundedAction.SHOW_WORKED_EXAMPLE.value,
                BoundedAction.SHOW_MICRO_LESSON.value,
            }:
                continue
            recalled_ids_by_intervention.setdefault(episode.intervention, []).append(
                episode.episode_id
            )
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
            recalled_episode_ids=[episode.episode_id for episode in recalled],
            recalled_successful_interventions=list(recalled_ids_by_intervention),
            recalled_episode_ids_by_intervention=recalled_ids_by_intervention,
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
            if lesson is not None and self._registry_matches_lesson(lesson=lesson):
                decision = decision.model_copy(
                    update={
                        "action_payload": {
                            **decision.action_payload,
                            "content_id": lesson["id"],
                            "content_version": lesson["version"],
                            "review_status": lesson["review_status"],
                            "content_hash": lesson["content_hash"],
                            "pack_version": lesson["pack_version"],
                            "skill": lesson["target_skill"],
                            "misconception": misconception,
                            "license": lesson["license"],
                            "source_lineage": lesson["source_lineage"],
                        },
                        "content_id": lesson["id"],
                    }
                )
                referenced_content.append(lesson["id"])
            else:
                decision = decision.model_copy(
                    update={
                        "action": BoundedAction.RETRY_SAME_SKILL.value,
                        "action_payload": {
                            "skill": inputs.skill,
                            "difficulty": inputs.difficulty,
                        },
                        "content_id": None,
                        "reason_code": "CONTENT_UNAVAILABLE_FALLBACK",
                        "reason_text": (
                            "The selected teaching asset is unavailable or no "
                            "longer passes the approved-content gate, so the "
                            "student stays on the same skill with another question."
                        ),
                    }
                )
                result = PolicyResult(
                    decision=decision,
                    next_state=SessionState.QUESTION_ACTIVE,
                )
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
                    contradicted=contradicted,
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
            if (
                lesson is None
                or not lesson.get("content_hash")
                or not self._registry_matches_lesson(lesson=lesson)
            ):
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

    def _registry_matches_lesson(self, *, lesson: dict) -> bool:
        """Require the installed lesson to match the authoritative PG registry."""
        try:
            version = int(lesson["version"])
        except (KeyError, TypeError, ValueError):
            return False
        row = self.connection.execute(
            """
            SELECT ci.content_id, ci.version, ci.content_type, ci.target_skill,
                   ci.license_id, ci.license_name, ci.source_id,
                   ci.review_status, ci.status, ci.withdrawn_at,
                   ci.canonical_body_hash, ci.license_snapshot_json,
                   ci.source_lineage_json, civ.content_hash AS version_hash,
                   cp.pack_version, cp.status AS pack_status
            FROM content_items AS ci
            JOIN content_item_versions AS civ
              ON civ.content_id = ci.content_id
             AND civ.version = ci.version
            JOIN content_pack_items AS cpi
              ON cpi.content_id = ci.content_id
             AND cpi.version = ci.version
            JOIN content_packs AS cp
              ON cp.pack_id = cpi.pack_id
             AND cp.pack_version = %s
             AND cp.pack_id = %s
            WHERE ci.content_id = %s
              AND ci.version = %s
              AND ci.review_status = 'approved'
              AND ci.status = 'approved'
              AND ci.withdrawn_at IS NULL
              AND cp.status = 'published'
            LIMIT 1
            """,
            (lesson["pack_version"], lesson.get("pack_id"), lesson["id"], version),
        ).fetchone()
        if row is None:
            return False
        try:
            registry_license = json.loads(row["license_snapshot_json"] or "{}")
            registry_lineage = json.loads(row["source_lineage_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            return False
        return (
            row["content_id"] == lesson["id"]
            and int(row["version"]) == version
            and row["content_type"] == lesson["content_type"]
            and row["target_skill"] == lesson["target_skill"]
            and row["license_id"] == lesson.get("license_id")
            and row["license_name"] == lesson.get("license_name")
            and row["source_id"] == lesson.get("source_id")
            and row["version_hash"] == lesson["content_hash"]
            and row["canonical_body_hash"] == lesson["content_hash"]
            and registry_license == (lesson.get("license") or {})
            and registry_lineage == (lesson.get("source_lineage") or {})
            and row["pack_version"] == lesson["pack_version"]
        )

    def _shadow_intervention_evidence(
        self,
        student_id: str,
        skill: str,
        misconception: str | None,
        *,
        difficulty_band: str | None = None,
        recalled: list[Episode] | None = None,
    ) -> list[InterventionEvidence]:
        scoped_stats: list[tuple[dict, BoundedAction, InterventionStat]] = []
        for row in self._intervention_stats(student_id, in_transaction=True):
            if row["skill"] != skill or row["misconception"] != misconception:
                continue
            if difficulty_band is not None and row["difficulty_band"] != difficulty_band:
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
            scoped_stats.append((row, intervention, stat))

        entries: list[InterventionEvidence] = []
        # Window bound: at most 8 scoped stats are surfaced per decision. The
        # SQL is unscoped (all interventions for the student); rows are filtered
        # to one skill+misconception+difficulty band before ranking, so a wider
        # window would only ever add interventions outside the current question
        # context. The cap keeps the shadow material bounded.
        for row, intervention, stat in scoped_stats[:8]:
            effectiveness = stat.blended_effectiveness()
            alternative_effectiveness = [
                other_stat.blended_effectiveness()
                for other_row, _other_intervention, other_stat in scoped_stats
                if (
                    other_row["difficulty_band"] == row["difficulty_band"]
                    and other_row["intervention"] != row["intervention"]
                    and other_stat.blended_effectiveness() is not None
                )
            ]
            # Plan Section 12.4: a >=0.15 difference is required before a stat
            # is used as a *preference claim* over another teaching strategy.
            # When no same-band alternative has evidence, there is no stated
            # preference to protect: the gate stays fail-open on the gap and
            # support is decided by attempts/effectiveness/contradiction alone.
            has_material_effect_gap = not alternative_effectiveness or all(
                effectiveness is not None
                and effectiveness - alternative >= MIN_INTERVENTION_EFFECT_GAP
                for alternative in alternative_effectiveness
            )
            comparable_attempts = sum(
                row[f"{window}_attempts"]
                for window in ("immediate", "short_term", "delayed")
            )
            has_recent_contradiction = any(
                episode.skill == skill
                and episode.misconception == misconception
                and episode.intervention == row["intervention"]
                and (
                    episode.status == "contradicted"
                    or episode.outcome.get("correct") is False
                )
                for episode in recalled or []
            )
            supported = (
                comparable_attempts >= MIN_INTERVENTION_STAT_ATTEMPTS
                and effectiveness is not None
                and effectiveness >= MIN_INTERVENTION_SUPPORT
                and has_material_effect_gap
                and not has_recent_contradiction
            )
            entries.append(
                InterventionEvidence(
                    skill=row["skill"],
                    misconception=row["misconception"],
                    intervention=intervention,
                    difficulty_band=row["difficulty_band"],
                    immediate_attempts=row["immediate_attempts"],
                    short_term_attempts=row["short_term_attempts"],
                    delayed_attempts=row["delayed_attempts"],
                    blended_effectiveness=effectiveness if supported else None,
                    support="supported" if supported else "insufficient",
                )
            )
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
        contradicted: list[Episode] | None = None,
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
                    envelope.student_id,
                    meta["skill"],
                    inputs.active_misconception,
                    difficulty_band=f"d{meta.get('difficulty') or 2}",
                    recalled=[*recalled, *(contradicted or [])],
                ),
                content_candidates=candidates,
            )
        )
        evidence = AuthoritativeEvidence(
            episodes={episode.episode_id: episode for episode in recalled},
            content=records,
            expected_student_id=envelope.student_id,
        )
        token = self._decision_token(
            envelope=envelope,
            decision=decision,
            constraints=constraints,
            meta=meta,
        )
        return ShadowMaterial(
            source_event_id=envelope.event_id,
            context=context,
            constraints=constraints,
            evidence=evidence,
            fallback=decision,
            task=(
                HybridTask.ACTION_RANKING
                if task_enabled(HybridTask.ACTION_RANKING)
                else HybridTask.DECISION_REASONING
            ),
            token=token,
            verified_payloads=self._verified_payloads(
                inputs=inputs,
                decision=decision,
                constraints=constraints,
                pack_key=pack_key,
                meta=meta,
            ),
            explanation=self._build_explanation_context(
                inputs=inputs,
                decision=decision,
                recalled=recalled,
                pack_key=pack_key,
            ),
        )

    def _decision_token(
        self,
        *,
        envelope: SyncEventEnvelope,
        decision: AgentDecision,
        constraints: PolicyConstraints,
        meta: dict,
    ) -> DecisionToken | None:
        """Phase A boundary evidence for H7 Phase C revalidation. Derived from
        the durable state inside the authoritative transaction: the committed
        fallback identity, the post-transition session state, and the agent
        event count that includes the just-committed fallback event."""
        next_state = constraints.next_states.get(decision.action)
        if next_state is None:
            return None
        row = self.connection.execute(
            """
            SELECT session_state FROM study_sessions
            WHERE session_id = %s
              AND tenant_id = current_setting('app.tenant_id', true)
            """,
            (envelope.session_id,),
        ).fetchone()
        if row is None:
            return None
        count = self.connection.execute(
            """
            SELECT COUNT(*) AS n FROM agent_events
            WHERE student_id = %s
              AND session_id = %s
              AND tenant_id = current_setting('app.tenant_id', true)
            """,
            (envelope.student_id, envelope.session_id),
        ).fetchone()["n"]
        learning_event_count = self.connection.execute(
            """
            SELECT COUNT(*) AS n FROM learning_events
            WHERE student_id = %s
              AND session_id = %s
              AND tenant_id = current_setting('app.tenant_id', true)
            """,
            (envelope.student_id, envelope.session_id),
        ).fetchone()["n"]
        return DecisionToken(
            student_id=envelope.student_id,
            session_id=envelope.session_id,
            source_event_id=envelope.event_id,
            fallback_action=decision.action,
            reason_code=decision.reason_code,
            policy_version=decision.policy_version,
            state_after=next_state.value,
            agent_event_count=int(count),
            learning_event_count=int(learning_event_count),
        )

    def _verified_payloads(
        self,
        *,
        inputs: PolicyInput,
        decision: AgentDecision,
        constraints: PolicyConstraints,
        pack_key,
        meta: dict,
    ) -> dict[str, dict]:
        """Deterministic payload for every allowed teaching action, derived
        inside the authoritative transaction (Phase A). Phase C serves one of
        these payloads unchanged: it never carries model-authored content."""
        lesson_type_by_action = {
            BoundedAction.SHOW_WORKED_EXAMPLE.value: "worked_example",
            BoundedAction.SHOW_MICRO_LESSON.value: "micro_lesson",
        }
        non_content_payload_by_action = {
            BoundedAction.END_WITH_REVIEW.value: {"review": "time_budget"},
            BoundedAction.RETRY_SAME_SKILL.value: {
                "skill": inputs.skill,
                "difficulty": inputs.difficulty,
            },
            BoundedAction.SWITCH_TO_PREREQUISITE.value: {
                "skill": inputs.skill,
                "difficulty": inputs.difficulty,
            },
            BoundedAction.LOWER_DIFFICULTY.value: {
                "skill": inputs.skill,
                "difficulty": max(1, inputs.difficulty - 1),
            },
            BoundedAction.RAISE_DIFFICULTY.value: {
                "skill": inputs.skill,
                "difficulty": min(3, inputs.difficulty + 1),
            },
        }
        payloads: dict[str, dict] = {}
        for action_value in constraints.allowed_actions:
            action_name = action_value.value
            lesson_type = lesson_type_by_action.get(action_name)
            if lesson_type is None:
                payload = non_content_payload_by_action.get(action_name)
                if payload is not None:
                    payloads[action_name] = payload
                continue
            lesson = pack_key.teaching_asset_meta(
                meta["skill"], lesson_type, inputs.active_misconception
            )
            if (
                lesson is None
                or not lesson.get("content_hash")
                or not self._registry_matches_lesson(lesson=lesson)
            ):
                continue
            payloads[action_name] = {
                **decision.action_payload,
                "content_id": lesson["id"],
                "content_version": lesson["version"],
                "review_status": lesson["review_status"],
                "content_hash": lesson["content_hash"],
                "pack_version": lesson["pack_version"],
                "skill": lesson["target_skill"],
                "misconception": inputs.active_misconception,
                "license": lesson["license"],
                "source_lineage": lesson["source_lineage"],
            }
        return payloads

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
            intervention_label = {
                BoundedAction.SHOW_WORKED_EXAMPLE.value: "worked example",
                BoundedAction.SHOW_MICRO_LESSON.value: "micro lesson",
            }.get(episode.intervention)
            if intervention_label is None:
                continue
            facts.append(
                ExplanationFact(
                    ref=f"episode:{episode.episode_id}",
                    phrase=(
                        f"A validated prior {intervention_label} on this skill "
                        "was followed by success on a different item."
                    ),
                )
            )
        if evidence_counts >= 1 and misconception:
            misconception_label = misconception.replace("_", " ")
            facts.append(
                ExplanationFact(
                    ref="stat:misconception",
                    phrase=(
                        f"{evidence_counts} {misconception_label} mistake"
                        f"{'s' if evidence_counts != 1 else ''} were recorded "
                        "in this session"
                    ),
                )
            )
        if inputs.consecutive_errors >= 1:
            facts.append(
                ExplanationFact(
                    ref="stat:consecutive_errors",
                    phrase=(
                        f"{inputs.consecutive_errors} consecutive wrong answer"
                        f"{'s' if inputs.consecutive_errors != 1 else ''} were "
                        f"recorded on {inputs.skill.replace('_', ' ')}"
                    ),
                )
            )
        action = BoundedAction(decision.action)
        action_purpose = {
            BoundedAction.SHOW_WORKED_EXAMPLE: (
                "a worked example reviews the current error pattern before more practice"
            ),
            BoundedAction.SHOW_MICRO_LESSON: (
                "a micro lesson reviews the current skill concept before more practice"
            ),
        }[action]
        facts.append(
            ExplanationFact(
                ref="action:purpose",
                phrase=action_purpose,
            )
        )
        facts.append(
            ExplanationFact(
                ref="stat:mastery",
                phrase=f"current mastery is {inputs.mastery:.2f}",
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
    ) -> tuple[dict[str, ExplanationProposal], list[HybridShadowObservation]]:
        """Post-commit shadow gateway: verified decision observations and
        verified personalized explanations. Returns explanations keyed by
        source event id together with every observation produced; failures
        only suppress enrichment, never the executed deterministic response."""
        explanations: dict[str, ExplanationProposal] = {}
        observations: list[HybridShadowObservation] = []
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
                observations.append(observation)
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
        return explanations, observations

    def _apply_ranked_actions(
        self,
        response: SyncResponse,
        materials: list[ShadowMaterial],
        observations: list[HybridShadowObservation],
    ) -> None:
        """H7 Phase C: serve a verified action only after a short revalidation
        transaction confirms the Phase A decision token is still current.

        Every step fails closed: a stale token, an unverifiable payload,
        a persistence error or any exception keeps the deterministic fallback
        already present in the response, exactly as in H5."""
        by_source: dict[str, ShadowMaterial] = {
            material.source_event_id: material for material in materials
        }
        for observation in observations:
            if not (observation.accepted and observation.would_change):
                continue
            if observation.model_proposal_action is None:
                continue
            material = by_source.get(observation.source_event_id)
            if material is None or material.token is None:
                continue
            verified_action = observation.model_proposal_action.value
            verified_state = material.constraints.next_states.get(
                observation.model_proposal_action
            )
            if verified_state is None:
                logger.info(
                    "hybrid_action_rank missing verified state dropped "
                    "source_event_id=%s verified_action=%s",
                    observation.source_event_id,
                    verified_action,
                )
                continue
            payload = (material.verified_payloads or {}).get(verified_action)
            if payload is None:
                logger.info(
                    "hybrid_action_rank no verifiable payload for "
                    "source_event_id=%s verified=%s",
                    observation.source_event_id,
                    verified_action,
                )
                continue
            try:
                fresh = self._revalidate_and_persist_decision_trace(
                    material, observation
                )
            except Exception:
                logger.exception(
                    "hybrid action-ranking revalidation failed for "
                    "source_event_id=%s",
                    observation.source_event_id,
                )
                continue
            if not fresh:
                logger.info(
                    "hybrid_action_rank stale token dropped source_event_id=%s",
                    observation.source_event_id,
                )
                continue
            for event in response.server_events:
                if event["source_event_id"] == observation.source_event_id:
                    event["action"] = verified_action
                    event["action_payload"] = payload
                    event["state_after"] = verified_state.value
                    event["reason_code"] = "HYBRID_RANKED_ACTION"
                    event["reason_text"] = (
                        "A verified alternative teaching move was selected from "
                        "the allowed policy actions."
                    )
                    event["hybrid_ranked"] = True
                    event["decision_trace_id"] = self._trace_id(
                        observation.source_event_id
                    )

    def _revalidate_and_persist_decision_trace(
        self, material: ShadowMaterial, observation: HybridShadowObservation
    ) -> bool:
        """Atomically revalidate Phase A and persist the H7 trace.

        The student lock covers both operations so a concurrent sync cannot
        advance the token after validation but before trace persistence.
        """
        token: DecisionToken = material.token  # type: ignore[assignment]
        with student_advisory_lock(self.connection, token.student_id):
            with transaction(self.connection):
                verified_action = observation.model_proposal_action
                lesson_type = {
                    BoundedAction.SHOW_WORKED_EXAMPLE: "worked_example",
                    BoundedAction.SHOW_MICRO_LESSON: "micro_lesson",
                }.get(verified_action)
                if lesson_type is not None:
                    verified_payload = (material.verified_payloads or {}).get(
                        verified_action.value
                    )
                    if not isinstance(verified_payload, dict) or not isinstance(
                        verified_payload.get("pack_version"), str
                    ) or not self._lesson_matches_verified_payload(
                        pack_version=verified_payload["pack_version"],
                        lesson_type=lesson_type,
                        misconception=verified_payload.get("misconception"),
                        payload=verified_payload,
                    ):
                        return False
                if not self._decision_token_is_current(token):
                    return False
                self._insert_decision_trace(material, observation)
        return True

    def _decision_token_is_current(self, token: DecisionToken) -> bool:
        row = self.connection.execute(
            """
            SELECT action, reason_code, policy_version FROM agent_events
            WHERE student_id = %s
              AND session_id = %s
              AND source_event_id = %s
              AND tenant_id = current_setting('app.tenant_id', true)
            """,
            (token.student_id, token.session_id, token.source_event_id),
        ).fetchone()
        if row is None:
            return False
        if (row["action"], row["reason_code"], row["policy_version"]) != (
            token.fallback_action,
            token.reason_code,
            token.policy_version,
        ):
            return False
        session = self.connection.execute(
            """
            SELECT session_state FROM study_sessions
            WHERE session_id = %s
              AND tenant_id = current_setting('app.tenant_id', true)
            """,
            (token.session_id,),
        ).fetchone()
        if session is None or session["session_state"] != token.state_after:
            return False
        count = self.connection.execute(
            """
            SELECT COUNT(*) AS n FROM agent_events
            WHERE student_id = %s
              AND session_id = %s
              AND tenant_id = current_setting('app.tenant_id', true)
            """,
            (token.student_id, token.session_id),
        ).fetchone()["n"]
        learning_event_count = self.connection.execute(
            """
            SELECT COUNT(*) AS n FROM learning_events
            WHERE student_id = %s
              AND session_id = %s
              AND tenant_id = current_setting('app.tenant_id', true)
            """,
            (token.student_id, token.session_id),
        ).fetchone()["n"]
        return (
            int(count) == token.agent_event_count
            and int(learning_event_count) == token.learning_event_count
        )

    @staticmethod
    def _trace_id(source_event_id: str) -> str:
        return f"h7b_{source_event_id}"

    def _insert_decision_trace(
        self, material: ShadowMaterial, observation: HybridShadowObservation
    ) -> None:
        """Insert the idempotent H7 trace within the caller's transaction."""
        token: DecisionToken = material.token  # type: ignore[assignment]
        self.connection.execute(
            """
            INSERT INTO hybrid_decision_trace (
                trace_id, tenant_id, student_id, source_event_id,
                decision_token, fallback_action, verified_action,
                accepted_checks, created_at
            ) VALUES (
                %s, current_setting('app.tenant_id', true), %s, %s, %s, %s, %s, %s, %s
            )
            ON CONFLICT (trace_id) DO NOTHING
            """,
            (
                self._trace_id(token.source_event_id),
                token.student_id,
                token.source_event_id,
                json.dumps(
                    {
                        "session_id": token.session_id,
                        "fallback_action": token.fallback_action,
                        "reason_code": token.reason_code,
                        "policy_version": token.policy_version,
                        "state_after": token.state_after,
                        "agent_event_count": token.agent_event_count,
                        "learning_event_count": token.learning_event_count,
                        "verified_action_payload": (
                            material.verified_payloads or {}
                        ).get(observation.model_proposal_action.value),
                    },
                    sort_keys=True,
                ),
                token.fallback_action,
                observation.model_proposal_action.value,
                json.dumps(list(observation.verification_checks), sort_keys=True),
                _utc_now_iso(),
            ),
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
