"""LearnerStore on PostgreSQL."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event

import pytest
from psycopg.errors import NotNullViolation

from app.domain.events import LearningEvent, LearningEventType
from app.domain.learner import MisconceptionState, SkillState
from app.domain.sessions import IllegalTransitionError, SessionState
from app.infrastructure import pg
from app.infrastructure.learner_store import (
    DuplicateEventIdError,
    LearnerStore,
)
from app.infrastructure.migration_runner import migrate_database


TENANT = "tenant_test"


@pytest.fixture()
def store() -> LearnerStore:
    admin = pg.connect_admin()
    try:
        migrate_database(admin)
    finally:
        admin.close()

    conn = pg.connect()
    try:
        conn.execute(
            "SELECT set_config('app.tenant_id', 'tenant_test', false)"
        )
        conn.commit()
        yield LearnerStore(conn)
    finally:
        conn.close()
        cleanup = pg.connect_admin()
        try:
            cleanup.execute("DROP SCHEMA public CASCADE")
            cleanup.execute("CREATE SCHEMA public")
            cleanup.commit()
        finally:
            cleanup.close()


def _create_question_session(store: LearnerStore) -> tuple[str, str]:
    student_id, _ = store.create_student("Ada", 30, 600)
    session_id = "session_test"
    store.create_session(student_id, session_id)
    for state in (
        SessionState.PROFILE_READY,
        SessionState.DIAGNOSTIC_ACTIVE,
        SessionState.DIAGNOSTIC_COMPLETE,
        SessionState.PLAN_READY,
        SessionState.QUESTION_ACTIVE,
    ):
        store.transition_session(session_id, state)
    return student_id, session_id


def _evaluation_event(
    student_id: str,
    session_id: str,
    event_id: str,
    *,
    occurred_at: str = "2026-01-01T00:00:00+00:00",
    correct: bool = True,
    event_type: LearningEventType = LearningEventType.ANSWER_EVALUATED,
    event_student_id: str | None = None,
    event_session_id: str | None = None,
    payload: dict[str, object] | None = None,
) -> LearningEvent:
    event_payload: dict[str, object] = {"correct": correct}
    if payload:
        event_payload.update(payload)
    return LearningEvent(
        event_id=event_id,
        student_id=event_student_id if event_student_id is not None else student_id,
        session_id=event_session_id if event_session_id is not None else session_id,
        event_type=event_type,
        payload=event_payload,
        occurred_at=occurred_at,
        received_at=occurred_at,
        origin="online",
    ).with_integrity()


def _record_evaluation(
    store: LearnerStore,
    *,
    student_id: str,
    session_id: str,
    event: LearningEvent,
    correct: bool = True,
    misconception: str | None = None,
    skill: str = "linear_equations",
    content_id: str = "math.linear_equations.001",
    content_version: int = 1,
    sequence: int = 1,
    selected_choice_id: str = "A",
    hint_level: int = 0,
    target_state: SessionState = SessionState.ANSWER_EVALUATED,
) -> tuple[object, SkillState | None]:
    return store.record_answer_evaluation(
        student_id=student_id,
        session_id=session_id,
        event=event,
        content_id=content_id,
        content_version=content_version,
        skill=skill,
        subskill="isolate_variable",
        difficulty=2,
        sequence=sequence,
        selected_choice_id=selected_choice_id,
        correct=correct,
        hint_level=hint_level,
        weight=1.0,
        validity="valid",
        misconception=misconception,
        misconception_source_label="distractor_mapping",
        misconception_confidence_label="high",
        session_state=target_state,
    )


class _SynchronizedConnection:
    """Coordinate one query while keeping the underlying connection independent."""

    def __init__(self, connection, query_fragment: str, barrier: Barrier) -> None:
        self._connection = connection
        self._query_fragment = query_fragment
        self._barrier = barrier

    def execute(self, query, params=None):
        normalized_query = " ".join(str(query).split()).upper()
        if self._query_fragment in normalized_query:
            self._barrier.wait(timeout=10)
        return self._connection.execute(query, params)

    def __getattr__(self, name):
        return getattr(self._connection, name)


class _MisconceptionRace:
    def __init__(self) -> None:
        self.allow_second_count = Event()
        self.first_count_finished = Event()
        self.release_first = Event()
        self.second_advisory_requested = Event()
        self.second_count_finished = Event()
        self.second_progress = Event()


class _MisconceptionSynchronizedConnection:
    """Coordinate the count without delaying the transaction by wall time."""

    def __init__(self, connection, participant: str, race: _MisconceptionRace) -> None:
        self._connection = connection
        self._participant = participant
        self._race = race

    def execute(self, query, params=None):
        normalized_query = " ".join(str(query).split()).upper()
        if (
            self._participant == "second"
            and "PG_ADVISORY_XACT_LOCK" in normalized_query
        ):
            self._race.second_advisory_requested.set()
            self._race.second_progress.set()

        if "SELECT COUNT(*) AS TOTAL" in normalized_query:
            if self._participant == "first":
                cursor = self._connection.execute(query, params)
                self._race.first_count_finished.set()
                if not self._race.release_first.wait(timeout=10):
                    raise TimeoutError("first misconception count was not released")
                return cursor

            if not self._race.allow_second_count.wait(timeout=10):
                raise TimeoutError("second misconception count was not released")
            cursor = self._connection.execute(query, params)
            self._race.second_count_finished.set()
            self._race.second_progress.set()
            return cursor

        return self._connection.execute(query, params)

    def __getattr__(self, name):
        return getattr(self._connection, name)


def test_create_student_emits_student_created_event(store: LearnerStore) -> None:
    student_id, event = store.create_student("Ada", 30, 600)

    assert event.student_id == student_id
    assert event.event_type is LearningEventType.STUDENT_CREATED
    row = store.connection.execute(
        "SELECT event_type FROM learning_events WHERE event_id = %s",
        (event.event_id,),
    ).fetchone()
    assert row["event_type"] == LearningEventType.STUDENT_CREATED.value


def test_session_create_get_and_transition_round_trip(store: LearnerStore) -> None:
    student_id, _ = store.create_student("Ada", 30, 600)
    event = store.create_session(student_id, "session_round_trip")

    assert event.event_type is LearningEventType.DIAGNOSTIC_STARTED
    assert store.get_session_state("session_round_trip") is SessionState.NEW

    assert (
        store.transition_session(
            "session_round_trip", SessionState.PROFILE_READY
        )
        is SessionState.PROFILE_READY
    )
    assert (
        store.get_session_state("session_round_trip")
        is SessionState.PROFILE_READY
    )


def test_record_answer_updates_skill_state_and_session(store: LearnerStore) -> None:
    student_id, session_id = _create_question_session(store)
    event = _evaluation_event(student_id, session_id, "event_skill")

    evidence, state = _record_evaluation(
        store,
        student_id=student_id,
        session_id=session_id,
        event=event,
    )

    assert evidence is None
    assert isinstance(state, SkillState)
    assert state.mastery > 0.5
    assert state.evidence_count == 1
    assert state.correct_streak == 1
    assert state.incorrect_streak == 0
    assert store.get_session_state(session_id) is SessionState.ANSWER_EVALUATED
    attempt = store.connection.execute(
        "SELECT correct, weight FROM answer_attempts WHERE event_id = %s",
        (event.event_id,),
    ).fetchone()
    assert attempt["correct"] == 1
    assert attempt["weight"] == 1.0


def test_create_session_requires_a_tenant_student(store: LearnerStore) -> None:
    with pytest.raises(KeyError, match="Unknown student missing"):
        store.create_session("missing", "session_missing")


def test_record_answer_rejects_a_session_owned_by_another_student(
    store: LearnerStore,
) -> None:
    _, session_id = _create_question_session(store)
    other_student_id, _ = store.create_student("Bea", 30, 600)
    event = _evaluation_event(other_student_id, session_id, "event_wrong_owner")

    with pytest.raises(ValueError, match="does not own session"):
        _record_evaluation(
            store,
            student_id=other_student_id,
            session_id=session_id,
            event=event,
        )

    assert store.connection.execute(
        "SELECT COUNT(*) AS total FROM learning_events WHERE event_id = %s",
        (event.event_id,),
    ).fetchone()["total"] == 0


def test_record_answer_rejects_a_tampered_event_hash(store: LearnerStore) -> None:
    student_id, session_id = _create_question_session(store)
    event = _evaluation_event(
        student_id,
        session_id,
        "event_tampered",
        payload={"tampered": True},
    ).model_copy(update={"payload": {"correct": True, "tampered": False}})

    with pytest.raises(ValueError, match="invalid integrity hash"):
        _record_evaluation(
            store,
            student_id=student_id,
            session_id=session_id,
            event=event,
        )

    assert store.connection.execute(
        "SELECT COUNT(*) AS total FROM learning_events WHERE event_id = %s",
        (event.event_id,),
    ).fetchone()["total"] == 0


@pytest.mark.parametrize(
    ("event_kwargs", "call_kwargs", "expected_fragment"),
    [
        pytest.param(
            {"event_type": LearningEventType.ANSWER_SUBMITTED},
            {},
            "event.event_type",
            id="event-type",
        ),
        pytest.param(
            {"event_student_id": "student_other"},
            {},
            "event.student_id",
            id="student-id",
        ),
        pytest.param(
            {"event_session_id": "session_other"},
            {},
            "event.session_id",
            id="session-id",
        ),
        pytest.param(
            {"payload": {"content_id": "math.other.001"}},
            {},
            "payload.content_id",
            id="content-id",
        ),
        pytest.param(
            {"payload": {"version": 2}},
            {},
            "payload.version",
            id="version",
        ),
        pytest.param(
            {"payload": {"selected_choice_id": "B"}},
            {},
            "payload.selected_choice_id",
            id="selected-choice-id",
        ),
        pytest.param(
            {"correct": False},
            {"correct": True},
            "payload.correct",
            id="correct",
        ),
        pytest.param(
            {"payload": {"hint_level": 1}},
            {},
            "payload.hint_level",
            id="hint-level",
        ),
    ],
)
def test_inconsistent_evaluation_inputs_reject_without_writes(
    store: LearnerStore,
    event_kwargs: dict[str, object],
    call_kwargs: dict[str, object],
    expected_fragment: str,
) -> None:
    student_id, session_id = _create_question_session(store)
    event = _evaluation_event(
        student_id,
        session_id,
        "event_inconsistent",
        **event_kwargs,
    )

    with pytest.raises(ValueError) as exc_info:
        _record_evaluation(
            store,
            student_id=student_id,
            session_id=session_id,
            event=event,
            misconception="sign_error",
            **call_kwargs,
        )

    assert expected_fragment in str(exc_info.value)
    assert store.connection.execute(
        "SELECT COUNT(*) AS total FROM learning_events WHERE event_id = %s",
        (event.event_id,),
    ).fetchone()["total"] == 0
    assert store.connection.execute(
        "SELECT COUNT(*) AS total FROM answer_attempts WHERE event_id = %s",
        (event.event_id,),
    ).fetchone()["total"] == 0
    assert store.get_skill_state(student_id, "linear_equations") is None
    assert store.connection.execute(
        """
        SELECT COUNT(*) AS total
        FROM misconception_evidence
        WHERE event_id = %s
        """,
        (event.event_id,),
    ).fetchone()["total"] == 0
    assert store.get_session_state(session_id) is SessionState.QUESTION_ACTIVE


def test_record_answer_projects_misconception_evidence(store: LearnerStore) -> None:
    student_id, session_id = _create_question_session(store)
    event = _evaluation_event(
        student_id, session_id, "event_misconception", correct=False
    )

    evidence, _ = _record_evaluation(
        store,
        student_id=student_id,
        session_id=session_id,
        event=event,
        correct=False,
        misconception="sign_error",
    )

    assert evidence is not None
    assert evidence.state is MisconceptionState.OBSERVED
    assert evidence.event_id == event.event_id
    assert store.count_misconception_evidence(
        student_id, "linear_equations", "sign_error"
    ) == (1, 1)


def test_misconception_state_counts_current_item_before_insert(
    store: LearnerStore,
) -> None:
    student_id, session_id = _create_question_session(store)

    first_event = _evaluation_event(
        student_id, session_id, "event_misconception_first", correct=False
    )
    first_evidence, _ = _record_evaluation(
        store,
        student_id=student_id,
        session_id=session_id,
        event=first_event,
        correct=False,
        misconception="sign_error",
        content_id="item_1",
        sequence=1,
    )
    assert first_evidence is not None
    assert first_evidence.state is MisconceptionState.OBSERVED

    store.transition_session(session_id, SessionState.QUESTION_ACTIVE)
    second_event = _evaluation_event(
        student_id, session_id, "event_misconception_second", correct=False
    )
    second_evidence, _ = _record_evaluation(
        store,
        student_id=student_id,
        session_id=session_id,
        event=second_event,
        correct=False,
        misconception="sign_error",
        content_id="item_1",
        sequence=2,
    )
    assert second_evidence is not None
    assert second_evidence.state is MisconceptionState.SUSPECTED

    store.transition_session(session_id, SessionState.QUESTION_ACTIVE)
    third_event = _evaluation_event(
        student_id, session_id, "event_misconception_third", correct=False
    )
    third_evidence, _ = _record_evaluation(
        store,
        student_id=student_id,
        session_id=session_id,
        event=third_event,
        correct=False,
        misconception="sign_error",
        content_id="item_2",
        sequence=3,
    )
    assert third_evidence is not None
    assert third_evidence.state is MisconceptionState.CONFIRMED

    rows = store.connection.execute(
        """
        SELECT event_id, state, item_id
        FROM misconception_evidence
        WHERE student_id = %s AND skill = %s AND misconception = %s
        ORDER BY event_id
        """,
        (student_id, "linear_equations", "sign_error"),
    ).fetchall()
    assert [
        (row["event_id"], row["state"], row["item_id"]) for row in rows
    ] == [
        (first_event.event_id, MisconceptionState.OBSERVED.value, "item_1"),
        (second_event.event_id, MisconceptionState.SUSPECTED.value, "item_1"),
        (third_event.event_id, MisconceptionState.CONFIRMED.value, "item_2"),
    ]
    assert store.count_misconception_evidence(
        student_id, "linear_equations", "sign_error"
    ) == (3, 2)


def test_duplicate_event_does_not_pollute_projections(
    store: LearnerStore,
) -> None:
    student_id, session_id = _create_question_session(store)
    event = _evaluation_event(
        student_id, session_id, "event_duplicate", correct=False
    )
    _record_evaluation(
        store,
        student_id=student_id,
        session_id=session_id,
        event=event,
        correct=False,
        misconception="sign_error",
    )

    with pytest.raises(DuplicateEventIdError):
        _record_evaluation(
            store,
            student_id=student_id,
            session_id=session_id,
            event=event,
            correct=False,
            misconception="sign_error",
        )

    state = store.get_skill_state(student_id, "linear_equations")
    assert state is not None
    assert state.evidence_count == 1
    assert store.count_misconception_evidence(
        student_id, "linear_equations", "sign_error"
    ) == (1, 1)
    assert store.connection.execute(
        "SELECT COUNT(*) AS total FROM answer_attempts WHERE event_id = %s",
        (event.event_id,),
    ).fetchone()["total"] == 1


def test_tenant_isolation_hides_session_skill_and_evidence(
    store: LearnerStore,
) -> None:
    student_id, session_id = _create_question_session(store)
    _record_evaluation(
        store,
        student_id=student_id,
        session_id=session_id,
        event=_evaluation_event(
            student_id, session_id, "event_tenant", correct=False
        ),
        correct=False,
        misconception="sign_error",
    )

    other_connection = pg.connect()
    try:
        other_connection.execute(
            "SELECT set_config('app.tenant_id', 'tenant_other', false)"
        )
        other_connection.commit()
        other_store = LearnerStore(other_connection)

        assert other_store.get_session_state(session_id) is None
        assert other_store.get_skill_state(student_id, "linear_equations") is None
        assert other_store.count_misconception_evidence(
            student_id, "linear_equations", "sign_error"
        ) == (0, 0)
    finally:
        other_connection.close()


def test_illegal_session_transition_does_not_update_projection(
    store: LearnerStore,
) -> None:
    student_id, _ = store.create_student("Ada", 30, 600)
    session_id = "session_illegal"
    store.create_session(student_id, session_id)

    with pytest.raises(IllegalTransitionError):
        store.transition_session(session_id, SessionState.QUESTION_ACTIVE)

    assert store.get_session_state(session_id) is SessionState.NEW


def test_unknown_session_transition_raises_key_error(store: LearnerStore) -> None:
    with pytest.raises(KeyError, match="Unknown session missing"):
        store.transition_session("missing", SessionState.PROFILE_READY)


def test_concurrent_transitions_recheck_locked_session_state(
    store: LearnerStore,
) -> None:
    student_id, session_id = _create_question_session(store)
    ready = Event()
    barrier = Barrier(2, action=ready.set)

    def transition_from_independent_connection() -> str:
        connection = pg.connect()
        try:
            connection.execute(
                "SELECT set_config('app.tenant_id', %s, false)",
                (TENANT,),
            )
            connection.commit()
            concurrent_store = LearnerStore(
                _SynchronizedConnection(
                    connection,
                    "SELECT SESSION_STATE FROM STUDY_SESSIONS",
                    barrier,
                )
            )
            try:
                concurrent_store.transition_session(
                    session_id, SessionState.ANSWER_EVALUATED
                )
            except IllegalTransitionError:
                return "illegal"
            return "success"
        finally:
            connection.close()

    lock_holder = pg.connect()
    try:
        lock_holder.execute(
            "SELECT set_config('app.tenant_id', %s, false)",
            (TENANT,),
        )
        lock_holder.commit()
        lock_holder.execute(
            "SELECT session_id FROM study_sessions WHERE session_id = %s FOR UPDATE",
            (session_id,),
        )

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(transition_from_independent_connection)
                for _ in range(2)
            ]
            try:
                assert ready.wait(timeout=10)
            finally:
                lock_holder.rollback()
            results = [future.result(timeout=10) for future in futures]
    finally:
        lock_holder.rollback()
        lock_holder.close()

    assert sorted(results) == ["illegal", "success"]
    assert store.get_session_state(session_id) is SessionState.ANSWER_EVALUATED


def test_concurrent_evaluations_lock_skill_projection(
    store: LearnerStore,
) -> None:
    student_id, first_session_id = _create_question_session(store)
    second_session_id = "session_concurrent_second"
    store.create_session(student_id, second_session_id)
    for state in (
        SessionState.PROFILE_READY,
        SessionState.DIAGNOSTIC_ACTIVE,
        SessionState.DIAGNOSTIC_COMPLETE,
        SessionState.PLAN_READY,
        SessionState.QUESTION_ACTIVE,
    ):
        store.transition_session(second_session_id, state)

    _record_evaluation(
        store,
        student_id=student_id,
        session_id=first_session_id,
        event=_evaluation_event(student_id, first_session_id, "event_seed"),
    )
    store.transition_session(first_session_id, SessionState.QUESTION_ACTIVE)

    ready = Event()
    barrier = Barrier(2, action=ready.set)

    def evaluate_from_independent_connection(
        session_id: str, event_id: str
    ) -> int:
        connection = pg.connect()
        try:
            connection.execute(
                "SELECT set_config('app.tenant_id', %s, false)",
                (TENANT,),
            )
            connection.commit()
            concurrent_store = LearnerStore(
                _SynchronizedConnection(
                    connection,
                    "SELECT ALPHA, BETA, EVIDENCE_COUNT, CORRECT_STREAK",
                    barrier,
                )
            )
            _, state = _record_evaluation(
                concurrent_store,
                student_id=student_id,
                session_id=session_id,
                event=_evaluation_event(student_id, session_id, event_id),
            )
            assert state is not None
            return state.evidence_count
        finally:
            connection.close()

    lock_holder = pg.connect()
    try:
        lock_holder.execute(
            "SELECT set_config('app.tenant_id', %s, false)",
            (TENANT,),
        )
        lock_holder.commit()
        lock_holder.execute(
            """
            SELECT student_id
            FROM student_skill_states
            WHERE student_id = %s AND skill = %s
            FOR UPDATE
            """,
            (student_id, "linear_equations"),
        )

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(
                    evaluate_from_independent_connection,
                    session_id,
                    event_id,
                )
                for session_id, event_id in (
                    (first_session_id, "event_concurrent_first"),
                    (second_session_id, "event_concurrent_second"),
                )
            ]
            try:
                assert ready.wait(timeout=10)
            finally:
                lock_holder.rollback()
            results = [future.result(timeout=10) for future in futures]
    finally:
        lock_holder.rollback()
        lock_holder.close()

    assert sorted(results) == [2, 3]
    state = store.get_skill_state(student_id, "linear_equations")
    assert state is not None
    assert state.alpha == 5.0
    assert state.evidence_count == 3
    assert state.correct_streak == 3


def test_concurrent_non_core_misconceptions_serialize_evidence_projection(
    store: LearnerStore,
) -> None:
    student_id, first_session_id = _create_question_session(store)
    second_session_id = "session_misconception_second"
    store.create_session(student_id, second_session_id)
    for state in (
        SessionState.PROFILE_READY,
        SessionState.DIAGNOSTIC_ACTIVE,
        SessionState.DIAGNOSTIC_COMPLETE,
        SessionState.PLAN_READY,
        SessionState.QUESTION_ACTIVE,
    ):
        store.transition_session(second_session_id, state)

    race = _MisconceptionRace()

    def evaluate_from_independent_connection(
        participant: str,
        session_id: str,
        event_id: str,
        content_id: str,
    ) -> object:
        connection = pg.connect()
        try:
            connection.execute(
                "SELECT set_config('app.tenant_id', %s, false)",
                (TENANT,),
            )
            connection.commit()
            concurrent_store = LearnerStore(
                _MisconceptionSynchronizedConnection(connection, participant, race)
            )
            evidence, _ = _record_evaluation(
                concurrent_store,
                student_id=student_id,
                session_id=session_id,
                event=_evaluation_event(
                    student_id, session_id, event_id, correct=False
                ),
                correct=False,
                misconception="sign_error",
                skill="non_core_skill",
                content_id=content_id,
            )
            assert evidence is not None
            return evidence
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(
            evaluate_from_independent_connection,
            "first",
            first_session_id,
            "event_misconception_first",
            "math.non_core.001",
        )
        try:
            assert race.first_count_finished.wait(timeout=10)
            race.allow_second_count.set()
            second_future = executor.submit(
                evaluate_from_independent_connection,
                "second",
                second_session_id,
                "event_misconception_second",
                "math.non_core.002",
            )
            assert race.second_progress.wait(timeout=10)
            if not race.second_count_finished.is_set():
                assert race.second_advisory_requested.is_set()
        finally:
            race.release_first.set()
        first_evidence = first_future.result(timeout=10)
        second_evidence = second_future.result(timeout=10)

    assert first_evidence.state is MisconceptionState.OBSERVED
    assert second_evidence.state is MisconceptionState.SUSPECTED
    assert store.count_misconception_evidence(
        student_id, "non_core_skill", "sign_error"
    ) == (2, 2)


def test_record_answer_rolls_back_after_answer_attempt_constraint_failure(
    store: LearnerStore,
) -> None:
    student_id, session_id = _create_question_session(store)
    event = _evaluation_event(student_id, session_id, "event_failed_attempt")

    with pytest.raises(NotNullViolation):
        _record_evaluation(
            store,
            student_id=student_id,
            session_id=session_id,
            event=event,
            content_id=None,
            misconception="sign_error",
        )

    assert store.connection.execute(
        "SELECT COUNT(*) AS total FROM learning_events WHERE event_id = %s",
        (event.event_id,),
    ).fetchone()["total"] == 0
    assert store.connection.execute(
        "SELECT COUNT(*) AS total FROM answer_attempts WHERE event_id = %s",
        (event.event_id,),
    ).fetchone()["total"] == 0
    assert store.get_skill_state(student_id, "linear_equations") is None
    assert store.count_misconception_evidence(
        student_id, "linear_equations", "sign_error"
    ) == (0, 0)
    assert store.get_session_state(session_id) is SessionState.QUESTION_ACTIVE

    _, state = _record_evaluation(
        store,
        student_id=student_id,
        session_id=session_id,
        event=_evaluation_event(student_id, session_id, "event_after_rollback"),
    )
    assert state is not None
    assert state.evidence_count == 1
    assert store.get_session_state(session_id) is SessionState.ANSWER_EVALUATED
