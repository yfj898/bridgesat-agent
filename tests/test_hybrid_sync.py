"""H4 real-path shadow integration tests (Hybrid Integration Plan H4).

The shadow gateway sits on the real /v1/sync/events path (SyncService.
process_batch): the deterministic answer/AgentEvent commits inside the
transaction, then a gated, bounded model call may produce a sanitized shadow
observation that never changes the executed action. These tests prove:

- flags off: zero shadow overhead and zero model calls;
- deterministic fast paths (single allowed action) never call the model;
- on the ambiguous branch the model is called AFTER the authoritative
  AgentEvent is committed and visible from another connection;
- a model failure or malformed output leaves the response unchanged;
- duplicate sync does not create a second authoritative decision or a
  second shadow observation.
"""

from __future__ import annotations

import hashlib
import json

import psycopg
import pytest

from app.agent.hybrid_contracts import HybridShadowObservation
from app.agent.llm_client import LLMClient
from app.domain.memory import BoundedAction, Episode
from app.infrastructure import pg
from app.sync.protocol import SyncRequest
from app.sync.service import SyncService
from tests.pg_test_helpers import import_fixture_pack

PACK_VERSION = "0.1.0"
Q_LINEAR = "sync.linear.001"

STUDENT_ID = "student_01"
DEVICE_A = "device_a"
SESSION_ID = "session_01"


def _integrity(event_type: str, payload: dict) -> str:
    digest = hashlib.sha256()
    digest.update(event_type.encode("utf-8"))
    digest.update(b"\x00")
    digest.update(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    return f"sha256:{digest.hexdigest()}"


def _envelope(
    *,
    event_id: str,
    device_sequence: int = 1,
    selected_choice_id: str = "B",
    question_id: str | None = Q_LINEAR,
) -> dict:
    payload = {
        "question_id": question_id,
        "question_version": 1,
        "selected_choice_id": selected_choice_id,
        "hint_level": 0,
        "attempt_id": event_id,
    }
    envelope = {
        "event_id": event_id,
        "student_id": STUDENT_ID,
        "session_id": SESSION_ID,
        "session_branch_id": "branch_" + DEVICE_A,
        "device_id": DEVICE_A,
        "device_sequence": device_sequence,
        "event_type": "ANSWER_SUBMITTED",
        "payload": payload,
        "content_pack_version": PACK_VERSION,
        "question_id": question_id,
        "question_version": 1,
        "policy_version": "offline-policy-v1",
        "depends_on_event_ids": [],
        "device_occurred_at": "2026-08-07T16:00:00+08:00",
        "integrity_hash": _integrity("ANSWER_SUBMITTED", payload),
    }
    return envelope


def _seed_student(service: SyncService) -> None:
    from app.domain.events import compute_integrity_hash, utc_now_iso
    from app.infrastructure.pg import transaction

    now = utc_now_iso()
    event = {
        "event_id": f"evt_seed_{STUDENT_ID}",
        "student_id": STUDENT_ID,
        "session_id": "",
        "event_type": "STUDENT_CREATED",
        "payload": {"name": "Test Student", "daily_minutes": 20, "target_score": 1200},
        "policy_version": "policy-0.1.0",
        "content_version": None,
        "occurred_at": now,
        "received_at": now,
        "device_id": None,
        "device_sequence": None,
        "origin": "online",
        "integrity_hash": compute_integrity_hash(
            "STUDENT_CREATED",
            {"name": "Test Student", "daily_minutes": 20, "target_score": 1200},
        ),
    }
    with transaction(service.connection):
        service.connection.execute(
            """
            INSERT INTO students (
                tenant_id, id, name, daily_minutes, target_score, mastery_json,
                status, created_at, updated_at
            ) VALUES (
                current_setting('app.tenant_id'), %s, %s, %s, %s, '{}', 'active', %s, %s
            )
            """,
            (STUDENT_ID, "Test Student", 20, 1200, now, now),
        )
        service.connection.execute(
            """
            INSERT INTO learning_events (
                tenant_id, event_id, student_id, session_id, event_type, payload_json,
                policy_version, content_version, occurred_at, received_at,
                device_id, device_sequence, origin, integrity_hash
            ) VALUES (
                current_setting('app.tenant_id'), %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s
            )
            """,
            (
                event["event_id"], event["student_id"], event["session_id"],
                event["event_type"], json.dumps(event["payload"]),
                event["policy_version"], event["content_version"],
                event["occurred_at"], event["received_at"],
                event["device_id"], event["device_sequence"],
                event["origin"], event["integrity_hash"],
            ),
        )


def _seed_repeated_misconception(service: SyncService) -> None:
    """Seed two prior misconception observations on distinct items so the
    policy enters the repeated-misconception branch (allowed_actions has
    three entries) on the next wrong answer."""
    now = "2026-08-07T15:00:00+08:00"
    for index, item_id in enumerate(("seed.item.alpha", "seed.item.beta")):
        service.connection.execute(
            """
            INSERT INTO misconception_evidence (
                tenant_id, evidence_id, student_id, session_id, event_id, skill,
                subskill, misconception, source_label, confidence_label, state,
                item_id, item_version, observed_at
            ) VALUES (
                current_setting('app.tenant_id'), %s, %s, %s, %s, %s, %s,
                %s, 'offline_distractor', 'high', 'confirmed_offline',
                %s, %s, %s
            )
            """,
            (
                f"evid_seed_{index}",
                STUDENT_ID,
                SESSION_ID,
                f"evt_seed_{index}",
                "linear_equations",
                "isolate_variables",
                "sign_error",
                item_id,
                1,
                now,
            ),
        )
    service.connection.commit()


class ChatTransport:
    def __init__(self, content: str | None = None, *, fail: bool = False) -> None:
        self.calls: list[tuple[str, dict, int]] = []
        self.content = content
        self.fail = fail

    async def request(self, url: str, body: dict, timeout_ms: int) -> dict:
        self.calls.append((url, body, timeout_ms))
        if self.fail:
            from app.agent.llm_client import LLMUnavailableError

            raise LLMUnavailableError("provider unavailable")
        return {"choices": [{"message": {"role": "assistant", "content": self.content}}]}


def _fake_client(transport: ChatTransport) -> LLMClient:
    return LLMClient(api_key="nvapi-test", model="test/model", transport=transport)


def _stat_row(
    intervention: BoundedAction,
    *,
    immediate_correct: float,
    immediate_attempts: int = 3,
) -> dict:
    return {
        "skill": "linear_equations",
        "misconception": "sign_error",
        "intervention": intervention.value,
        "difficulty_band": "d2",
        "immediate_correct": immediate_correct,
        "immediate_attempts": immediate_attempts,
        "immediate_weight": float(immediate_attempts),
        "short_term_correct": 0.0,
        "short_term_attempts": 0,
        "short_term_weight": 0.0,
        "delayed_correct": 0.0,
        "delayed_attempts": 0,
        "delayed_weight": 0.0,
    }


def _failed_recent_episode(intervention: BoundedAction) -> Episode:
    return Episode(
        episode_id="episode_failed_recent",
        student_id=STUDENT_ID,
        session_id=SESSION_ID,
        skill="linear_equations",
        misconception="sign_error",
        intervention=intervention.value,
        outcome={"correct": False},
        effectiveness=0.0,
        evidence_event_ids=["event_failed_recent"],
        summary="Recent attempt did not transfer.",
        confidence=0.8,
        status="validated",
    )


def test_shadow_stat_support_requires_effective_outcome(monkeypatch) -> None:
    service = object.__new__(SyncService)
    rows = [
        _stat_row(BoundedAction.SHOW_MICRO_LESSON, immediate_correct=1.0),
    ]
    monkeypatch.setattr(
        service,
        "_intervention_stats",
        lambda _student_id, in_transaction: rows,
    )

    evidence = service._shadow_intervention_evidence(
        STUDENT_ID, "linear_equations", "sign_error"
    )

    assert evidence[0].support == "insufficient"


def test_shadow_stat_support_requires_material_effect_gap(monkeypatch) -> None:
    service = object.__new__(SyncService)
    rows = [
        _stat_row(BoundedAction.SHOW_MICRO_LESSON, immediate_correct=2.4),
        _stat_row(BoundedAction.SHOW_WORKED_EXAMPLE, immediate_correct=2.1),
    ]
    monkeypatch.setattr(
        service,
        "_intervention_stats",
        lambda _student_id, in_transaction: rows,
    )

    evidence = service._shadow_intervention_evidence(
        STUDENT_ID, "linear_equations", "sign_error"
    )

    assert all(entry.support == "insufficient" for entry in evidence)


def test_shadow_stat_support_rejects_recent_contradiction(monkeypatch) -> None:
    service = object.__new__(SyncService)
    rows = [
        _stat_row(BoundedAction.SHOW_MICRO_LESSON, immediate_correct=2.7),
    ]
    monkeypatch.setattr(
        service,
        "_intervention_stats",
        lambda _student_id, in_transaction: rows,
    )

    evidence = service._shadow_intervention_evidence(
        STUDENT_ID,
        "linear_equations",
        "sign_error",
        recalled=[_failed_recent_episode(BoundedAction.SHOW_MICRO_LESSON)],
    )

    assert evidence[0].support == "insufficient"


def test_shadow_stat_support_is_difficulty_scoped(monkeypatch) -> None:
    service = object.__new__(SyncService)
    rows = [
        {
            **_stat_row(BoundedAction.SHOW_MICRO_LESSON, immediate_correct=3.0),
            "difficulty_band": "d3",
        },
    ]
    monkeypatch.setattr(
        service,
        "_intervention_stats",
        lambda _student_id, in_transaction: rows,
    )

    evidence = service._shadow_intervention_evidence(
        STUDENT_ID, "linear_equations", "sign_error", difficulty_band="d2"
    )

    assert evidence == []


def _shadow_flags_on(monkeypatch) -> None:
    monkeypatch.setenv("BRIDGESAT_HYBRID_ENABLED", "1")
    monkeypatch.setenv("BRIDGESAT_HYBRID_SHADOW_ENABLED", "1")


def _make_service(
    isolated_pg_database,
    client: LLMClient | None = None,
) -> SyncService:
    import_fixture_pack(isolated_pg_database.admin_dsn)
    connection = isolated_pg_database.connect_app()
    connection.execute(
        "SELECT set_config('app.tenant_id', %s, false)",
        (isolated_pg_database.tenant_id,),
    )
    connection.commit()
    service = SyncService(connection, llm_client=client)
    _seed_student(service)
    service.register_device(STUDENT_ID, "device a", device_id=DEVICE_A)
    return service


def _request(*event_ids_and_sequences: tuple[str, int]) -> SyncRequest:
    return SyncRequest.model_validate(
        {
            "student_id": STUDENT_ID,
            "device_id": DEVICE_A,
            "events": [
                _envelope(event_id=event_id, device_sequence=sequence)
                for event_id, sequence in event_ids_and_sequences
            ],
        }
    )


def _proposal_json(action: str = "RETRY_SAME_SKILL") -> str:
    return json.dumps(
        {
            "proposed_action": action,
            "selected_episode_id": None,
            "selected_content_id": None,
            "rationale_code": "CONTINUE_PRACTICE",
            "rationale": "One more distinct item gathers confirmation evidence.",
            "confidence": 0.7,
            "evidence_claims": [],
        }
    )


def _explanation_json() -> str:
    return json.dumps(
        {
            "student_explanation": (
                "Because 3 sign error mistakes were recorded in this "
                "session, a worked example shows the error pattern before "
                "more practice."
            ),
            "emphasis": "process",
            "evidence_refs": ["stat:misconception"],
        }
    )


# ---------------------------------------------------------------------------
# Flags off: no shadow overhead
# ---------------------------------------------------------------------------


def test_flags_off_no_model_call_and_deterministic_action(
    isolated_pg_database, monkeypatch
) -> None:
    monkeypatch.setenv("BRIDGESAT_HYBRID_ENABLED", "0")
    transport = ChatTransport(content=_proposal_json("SHOW_MICRO_LESSON"))
    service = _make_service(isolated_pg_database, client=_fake_client(transport))
    _seed_repeated_misconception(service)
    observations: list[HybridShadowObservation] = []
    service.on_shadow_observation = observations.append

    response = service.process_batch(_request(("evt_01", 1)))

    assert response.accepted_event_ids == ["evt_01"]
    action = response.server_events[0]["action"]
    assert action == "SHOW_WORKED_EXAMPLE"
    assert transport.calls == []
    assert observations == []


def test_deterministic_fast_path_never_calls_model_with_flags_on(
    isolated_pg_database, monkeypatch
) -> None:
    _shadow_flags_on(monkeypatch)
    transport = ChatTransport(content=_proposal_json())
    service = _make_service(isolated_pg_database, client=_fake_client(transport))
    observations: list[HybridShadowObservation] = []
    service.on_shadow_observation = observations.append

    response = service.process_batch(_request(("evt_02", 1)))

    assert response.server_events[0]["action"] == "RETRY_SAME_SKILL"
    assert transport.calls == []
    assert observations == []


# ---------------------------------------------------------------------------
# Ambiguous branch: shadow runs after commit and never changes the response
# ---------------------------------------------------------------------------


def test_shadow_runs_after_commit_and_observes_without_executing(
    isolated_pg_database, monkeypatch
) -> None:
    _shadow_flags_on(monkeypatch)
    transport = ChatTransport(content=_proposal_json("SHOW_MICRO_LESSON"))
    service = _make_service(isolated_pg_database, client=_fake_client(transport))
    _seed_repeated_misconception(service)
    observations: list[HybridShadowObservation] = []
    service.on_shadow_observation = observations.append

    response = service.process_batch(_request(("evt_03", 1)))

    fallback_action = response.server_events[0]["action"]
    assert fallback_action == "SHOW_WORKED_EXAMPLE"
    assert len(transport.calls) == 1
    assert len(observations) == 1
    observation = observations[0]
    assert observation.source_event_id == "evt_03"
    assert observation.fallback_action.value == fallback_action
    assert observation.model_proposal_action.value == "SHOW_MICRO_LESSON"
    assert observation.accepted is True
    assert observation.would_change is True
    assert observation.latency_ms >= 0


def test_agent_event_committed_before_model_is_called(
    isolated_pg_database, monkeypatch
) -> None:
    _shadow_flags_on(monkeypatch)

    async def verify_committed(url: str, body: dict, timeout_ms: int) -> dict:
        probe = pg.connect(isolated_pg_database.app_dsn)
        try:
            probe.execute(
                "SELECT set_config('app.tenant_id', %s, false)",
                (isolated_pg_database.tenant_id,),
            )
            count = probe.execute(
                """
                SELECT COUNT(*) AS total
                FROM agent_events
                WHERE source_event_id = %s
                """,
                ("evt_04",),
            ).fetchone()["total"]
            if count != 1:
                raise AssertionError(
                    f"expected exactly one committed agent event, found {count}"
                )
        finally:
            probe.close()
        return {
            "choices": [
                {"message": {"role": "assistant", "content": _proposal_json()}}
            ]
        }

    transport = ChatTransport()
    transport.request = verify_committed
    service = _make_service(isolated_pg_database, client=_fake_client(transport))
    _seed_repeated_misconception(service)

    response = service.process_batch(_request(("evt_04", 1)))

    assert response.accepted_event_ids == ["evt_04"]


def test_model_failure_leaves_response_unchanged(
    isolated_pg_database, monkeypatch
) -> None:
    _shadow_flags_on(monkeypatch)
    transport = ChatTransport(fail=True)
    service = _make_service(isolated_pg_database, client=_fake_client(transport))
    _seed_repeated_misconception(service)
    observations: list[HybridShadowObservation] = []
    service.on_shadow_observation = observations.append

    response = service.process_batch(_request(("evt_05", 1)))

    assert response.accepted_event_ids == ["evt_05"]
    assert response.server_events[0]["action"] == "SHOW_WORKED_EXAMPLE"
    assert len(observations) == 1
    assert observations[0].accepted is False
    assert observations[0].rejection_reason == "model_unavailable"
    assert observations[0].would_change is False


def test_duplicate_sync_creates_no_second_decision_or_observation(
    isolated_pg_database, monkeypatch
) -> None:
    _shadow_flags_on(monkeypatch)
    transport = ChatTransport(content=_proposal_json("SHOW_MICRO_LESSON"))
    service = _make_service(isolated_pg_database, client=_fake_client(transport))
    _seed_repeated_misconception(service)
    observations: list[HybridShadowObservation] = []
    service.on_shadow_observation = observations.append
    request = _request(("evt_06", 1))

    first = service.process_batch(request)
    second = service.process_batch(request)

    assert first.accepted_event_ids == ["evt_06"]
    assert second.accepted_event_ids == []
    assert second.duplicate_event_ids == ["evt_06"]
    row = service.connection.execute(
        """
        SELECT COUNT(*) AS total
        FROM agent_events
        WHERE source_event_id = %s
        """,
        ("evt_06",),
    ).fetchone()["total"]
    assert row == 1
    assert len(observations) == 1


# ---------------------------------------------------------------------------
# H5: verified personalized explanation enriches the response post-commit
# ---------------------------------------------------------------------------


def test_explanation_enriches_response_after_commit_without_changing_action(
    isolated_pg_database, monkeypatch
) -> None:
    _shadow_flags_on(monkeypatch)
    monkeypatch.setenv("BRIDGESAT_HYBRID_EXPLANATION_ENABLED", "1")
    transport = ChatTransport(content=_explanation_json())
    service = _make_service(isolated_pg_database, client=_fake_client(transport))
    _seed_repeated_misconception(service)

    response = service.process_batch(_request(("evt_07", 1)))

    event = response.server_events[0]
    assert event["action"] == "SHOW_WORKED_EXAMPLE"
    assert event["reason_code"] == "REPEATED_MISCONCEPTION"
    assert event["personalized_explanation"] == (
        "Because 3 sign error mistakes were recorded in this session, a "
        "worked example shows the error pattern before more practice."
    )
    assert event["personalized_emphasis"] == "process"
    assert len(transport.calls) == 2


def test_explanation_absent_when_model_unavailable(
    isolated_pg_database, monkeypatch
) -> None:
    _shadow_flags_on(monkeypatch)
    monkeypatch.setenv("BRIDGESAT_HYBRID_EXPLANATION_ENABLED", "1")
    transport = ChatTransport(fail=True)
    service = _make_service(isolated_pg_database, client=_fake_client(transport))
    _seed_repeated_misconception(service)

    response = service.process_batch(_request(("evt_08", 1)))

    event = response.server_events[0]
    assert event["action"] == "SHOW_WORKED_EXAMPLE"
    assert "personalized_explanation" not in event
    assert "personalized_emphasis" not in event
    assert len(transport.calls) == 2


def test_explanation_absent_when_flag_off(
    isolated_pg_database, monkeypatch
) -> None:
    monkeypatch.setenv("BRIDGESAT_HYBRID_ENABLED", "0")
    monkeypatch.setenv("BRIDGESAT_HYBRID_EXPLANATION_ENABLED", "1")
    transport = ChatTransport(content=_explanation_json())
    service = _make_service(isolated_pg_database, client=_fake_client(transport))
    _seed_repeated_misconception(service)

    response = service.process_batch(_request(("evt_09", 1)))

    event = response.server_events[0]
    assert event["action"] == "SHOW_WORKED_EXAMPLE"
    assert "personalized_explanation" not in event
    assert transport.calls == []
