"""H7 action ranking real-path integration tests (Hybrid Integration Plan
section 22).

H7 is the conditional action-changing phase: only under
``BRIDGESAT_HYBRID_ACTION_RANKING_ENABLED`` may a verified model proposal
replace the action in a sync response, and only through the bounded
two-phase path:

- Phase A: the deterministic fallback AgentEvent commits inside the
  advisory-lock transaction; a decision token (fallback identity, session
  state, agent event count) is captured with the shadow material.
- Phase B: the model call happens after the advisory lock is released.
- Phase C: a short revalidation transaction must reproduce the token exactly
  before a decision trace row is persisted and the verified action is served.

These tests prove:

- action ranking off by default: verified proposals never change the
  response (H5 behavior holds) and no trace is persisted;
- ranking on: a verified allowed action replaces the fallback in the
  response only, the durable AgentEvent stays the deterministic fallback,
  and one auditable trace row is persisted per source event;
- stale tokens (a learner advanced between Phase A and Phase C) keep the
  fallback and persist nothing;
- rejected, illegal, or unavailable proposals keep the fallback;
- duplicate syncs are idempotent (single trace, single model call);
- the model call never happens inside the advisory lock;
- a single fresh episode stays fully deterministic even with ranking on.
"""

from __future__ import annotations

import hashlib
import json
import time

import pytest

from app.agent.hybrid_contracts import HybridShadowObservation
from app.agent.llm_client import LLMClient, LLMUnavailableError
from app.infrastructure import pg
from app.sync.protocol import SyncRequest
from app.sync.service import SyncService

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
    return {
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
    """Two prior sign-error observations on distinct items: the policy enters
    the repeated-misconception branch (allowed_actions has three entries,
    deterministic fallback SHOW_WORKED_EXAMPLE) on the next wrong answer."""
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
    """Replay transport: optionally runs a callback per call (race
    simulation), then returns the next scripted content. Exhausted or failing
    scripts fail closed as unavailable."""

    def __init__(
        self,
        contents: list[str] | None = None,
        *,
        callback=None,
        fail: bool = False,
        record_time: bool = False,
    ) -> None:
        self.contents: list[str] = list(contents or [])
        self.calls: list[tuple[str, dict, int]] = []
        self.call_times: list[float] = []
        self.callback = callback
        self.fail = fail
        self.record_time = record_time

    async def request(self, url: str, body: dict, timeout_ms: int) -> dict:
        if self.record_time:
            self.call_times.append(time.monotonic())
        self.calls.append((url, body, timeout_ms))
        if self.callback is not None:
            self.callback()
        if self.fail or not self.contents:
            raise LLMUnavailableError("provider unavailable")
        content = self.contents.pop(0)
        return {"choices": [{"message": {"role": "assistant", "content": content}}]}


def _fake_client(transport: ChatTransport) -> LLMClient:
    return LLMClient(api_key="nvapi-test", model="test/model", transport=transport)


def _shadow_flags_on(monkeypatch) -> None:
    monkeypatch.setenv("BRIDGESAT_HYBRID_ENABLED", "1")
    monkeypatch.setenv("BRIDGESAT_HYBRID_SHADOW_ENABLED", "1")


def _ranking_flags_on(monkeypatch) -> None:
    _shadow_flags_on(monkeypatch)
    monkeypatch.setenv("BRIDGESAT_HYBRID_ACTION_RANKING_ENABLED", "1")


def _make_service(
    isolated_pg_database,
    client: LLMClient | None = None,
) -> SyncService:
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


def _agent_event_row(service: SyncService, event_id: str) -> dict:
    row = service.connection.execute(
        """
        SELECT action, action_payload_json, reason_code, policy_version
        FROM agent_events
        WHERE student_id = %s AND source_event_id = %s
          AND tenant_id = current_setting('app.tenant_id', true)
        """,
        (STUDENT_ID, event_id),
    ).fetchone()
    service.connection.commit()
    return row


def _trace_rows(service: SyncService) -> list[dict]:
    rows = service.connection.execute(
        """
        SELECT trace_id, source_event_id, student_id, decision_token,
               fallback_action, verified_action, accepted_checks
        FROM hybrid_decision_trace
        WHERE tenant_id = current_setting('app.tenant_id', true)
        ORDER BY created_at
        """
    ).fetchall()
    service.connection.commit()
    return list(rows)


# ---------------------------------------------------------------------------
# Off by default: H5 behavior holds exactly
# ---------------------------------------------------------------------------


def test_ranking_off_by_default_keeps_deterministic_response(
    isolated_pg_database, monkeypatch
) -> None:
    _shadow_flags_on(monkeypatch)
    transport = ChatTransport(contents=[_proposal_json("SHOW_MICRO_LESSON")])
    service = _make_service(isolated_pg_database, client=_fake_client(transport))
    _seed_repeated_misconception(service)
    observations: list[HybridShadowObservation] = []
    service.on_shadow_observation = observations.append

    response = service.process_batch(_request(("evt_01", 1)))

    assert response.server_events[0]["action"] == "SHOW_WORKED_EXAMPLE"
    assert "hybrid_ranked" not in response.server_events[0]
    assert len(observations) == 1
    assert observations[0].accepted is True
    assert observations[0].would_change is True
    assert _trace_rows(service) == []
    assert _agent_event_row(service, "evt_01")["action"] == "SHOW_WORKED_EXAMPLE"


def test_ranking_requires_master_flag_and_shadow_gate(
    isolated_pg_database, monkeypatch
) -> None:
    monkeypatch.setenv("BRIDGESAT_HYBRID_ENABLED", "0")
    monkeypatch.setenv("BRIDGESAT_HYBRID_ACTION_RANKING_ENABLED", "1")
    transport = ChatTransport(contents=[_proposal_json("SHOW_MICRO_LESSON")])
    service = _make_service(isolated_pg_database, client=_fake_client(transport))
    _seed_repeated_misconception(service)

    response = service.process_batch(_request(("evt_01", 1)))

    assert response.server_events[0]["action"] == "SHOW_WORKED_EXAMPLE"
    assert transport.calls == []
    assert _trace_rows(service) == []


# ---------------------------------------------------------------------------
# Ranking on: verified action served, fallback stays durable
# ---------------------------------------------------------------------------


def test_verified_ranking_replaces_fallback_with_auditable_trace(
    isolated_pg_database, monkeypatch
) -> None:
    _ranking_flags_on(monkeypatch)
    transport = ChatTransport(contents=[_proposal_json("SHOW_MICRO_LESSON")])
    service = _make_service(isolated_pg_database, client=_fake_client(transport))
    _seed_repeated_misconception(service)

    response = service.process_batch(_request(("evt_01", 1)))

    event = response.server_events[0]
    assert event["action"] == "SHOW_MICRO_LESSON"
    assert event["hybrid_ranked"] is True
    assert event["decision_trace_id"] == "h7b_evt_01"
    assert event["action_payload"]["content_id"] == "ml_linear_001"
    assert event["action_payload"]["review_status"] == "approved"
    assert event["action_payload"]["license"] != {}
    assert event["action_payload"]["source_lineage"] != {}
    durable = _agent_event_row(service, "evt_01")
    assert durable["action"] == "SHOW_WORKED_EXAMPLE"
    assert durable["reason_code"] == "REPEATED_MISCONCEPTION"
    traces = _trace_rows(service)
    assert len(traces) == 1
    trace = traces[0]
    assert trace["trace_id"] == "h7b_evt_01"
    assert trace["source_event_id"] == "evt_01"
    assert trace["student_id"] == STUDENT_ID
    assert trace["fallback_action"] == "SHOW_WORKED_EXAMPLE"
    assert trace["verified_action"] == "SHOW_MICRO_LESSON"
    token = json.loads(trace["decision_token"])
    assert token["session_id"] == SESSION_ID
    assert token["agent_event_count"] == 1
    assert token["policy_version"] == durable["policy_version"]
    assert "action_allowed" in json.loads(trace["accepted_checks"])


def test_ranking_and_explanation_coexist(
    isolated_pg_database, monkeypatch
) -> None:
    _ranking_flags_on(monkeypatch)
    monkeypatch.setenv("BRIDGESAT_HYBRID_EXPLANATION_ENABLED", "1")
    transport = ChatTransport(
        contents=[_proposal_json("SHOW_MICRO_LESSON"), _explanation_json()]
    )
    service = _make_service(isolated_pg_database, client=_fake_client(transport))
    _seed_repeated_misconception(service)

    response = service.process_batch(_request(("evt_01", 1)))

    event = response.server_events[0]
    assert event["action"] == "SHOW_MICRO_LESSON"
    assert event["hybrid_ranked"] is True
    assert event["personalized_explanation"]


# ---------------------------------------------------------------------------
# Race-safety: token staleness and concurrent advancement
# ---------------------------------------------------------------------------


def test_stale_token_after_concurrent_advance_keeps_fallback(
    isolated_pg_database, monkeypatch
) -> None:
    """A learner advancing the session between Phase B and Phase C (a
    concurrent second sync) must invalidate the token: the fallback stays in
    the response and nothing is persisted."""
    _ranking_flags_on(monkeypatch)

    def advance_session() -> None:
        monkeypatch.delenv("BRIDGESAT_HYBRID_ENABLED")
        try:
            service.process_batch(_request(("evt_02", 2)))
        finally:
            monkeypatch.setenv("BRIDGESAT_HYBRID_ENABLED", "1")

    transport = ChatTransport(
        contents=[_proposal_json("SHOW_MICRO_LESSON")], callback=advance_session
    )
    service = _make_service(isolated_pg_database, client=_fake_client(transport))
    _seed_repeated_misconception(service)

    response = service.process_batch(_request(("evt_01", 1)))

    assert len(response.server_events) == 1
    assert response.server_events[0]["action"] == "SHOW_WORKED_EXAMPLE"
    assert "hybrid_ranked" not in response.server_events[0]
    assert _trace_rows(service) == []
    assert _agent_event_row(service, "evt_01")["action"] == "SHOW_WORKED_EXAMPLE"
    assert _agent_event_row(service, "evt_02")["action"] is not None


def test_duplicate_sync_is_idempotent_single_trace(
    isolated_pg_database, monkeypatch
) -> None:
    _ranking_flags_on(monkeypatch)
    transport = ChatTransport(contents=[_proposal_json("SHOW_MICRO_LESSON")])
    service = _make_service(isolated_pg_database, client=_fake_client(transport))
    _seed_repeated_misconception(service)

    first = service.process_batch(_request(("evt_01", 1)))
    second = service.process_batch(_request(("evt_01", 1)))

    assert first.server_events[0]["hybrid_ranked"] is True
    assert second.accepted_event_ids == []
    assert len(transport.calls) == 1
    assert len(_trace_rows(service)) == 1


# ---------------------------------------------------------------------------
# Failed proposals keep the fallback exactly
# ---------------------------------------------------------------------------


def test_rejected_verified_action_not_served(
    isolated_pg_database, monkeypatch
) -> None:
    _ranking_flags_on(monkeypatch)
    transport = ChatTransport(
        contents=[_proposal_json("RETAKE_QUIZ")],
    )
    service = _make_service(isolated_pg_database, client=_fake_client(transport))
    _seed_repeated_misconception(service)
    observations: list[HybridShadowObservation] = []
    service.on_shadow_observation = observations.append

    response = service.process_batch(_request(("evt_01", 1)))

    assert response.server_events[0]["action"] == "SHOW_WORKED_EXAMPLE"
    assert "hybrid_ranked" not in response.server_events[0]
    assert observations[0].accepted is False
    assert observations[0].rejection_reason is not None
    assert _trace_rows(service) == []


def test_model_unavailable_keeps_fallback(
    isolated_pg_database, monkeypatch
) -> None:
    _ranking_flags_on(monkeypatch)
    transport = ChatTransport(fail=True)
    service = _make_service(isolated_pg_database, client=_fake_client(transport))
    _seed_repeated_misconception(service)

    response = service.process_batch(_request(("evt_01", 1)))

    assert response.server_events[0]["action"] == "SHOW_WORKED_EXAMPLE"
    assert "hybrid_ranked" not in response.server_events[0]
    assert _trace_rows(service) == []


# ---------------------------------------------------------------------------
# Execution boundary: model call never inside the advisory lock
# ---------------------------------------------------------------------------


def test_model_call_never_inside_advisory_lock(
    isolated_pg_database, monkeypatch
) -> None:
    _ranking_flags_on(monkeypatch)

    class LockRecorder:
        def __init__(self) -> None:
            self.intervals: list[tuple[float, float]] = []
            self._entered: float | None = None

        def __enter__(self):
            self._entered = time.monotonic()
            return self

        def __exit__(self, *exc) -> bool:
            self.intervals.append((self._entered, time.monotonic()))
            return False

    recorder = LockRecorder()

    def recording_lock(connection, student_id):
        return recorder

    monkeypatch.setattr("app.sync.service.student_advisory_lock", recording_lock)
    transport = ChatTransport(
        contents=[_proposal_json("SHOW_MICRO_LESSON")], record_time=True
    )
    service = _make_service(isolated_pg_database, client=_fake_client(transport))
    _seed_repeated_misconception(service)

    service.process_batch(_request(("evt_01", 1)))

    assert len(transport.call_times) == 1
    assert recorder.intervals
    for call_time in transport.call_times:
        for entered, exited in recorder.intervals:
            assert not (entered <= call_time <= exited), (
                "model call happened while the advisory lock was held"
            )


# ---------------------------------------------------------------------------
# Decisive one-episode demo stays deterministic with ranking on
# ---------------------------------------------------------------------------


def test_one_episode_demo_stays_deterministic_with_ranking(
    isolated_pg_database, monkeypatch
) -> None:
    _ranking_flags_on(monkeypatch)
    transport = ChatTransport(contents=[_proposal_json("SHOW_MICRO_LESSON")])
    service = _make_service(isolated_pg_database, client=_fake_client(transport))

    response = service.process_batch(_request(("evt_01", 1)))

    assert transport.calls == []
    assert response.server_events[0]["action"] == "RETRY_SAME_SKILL"
    assert "hybrid_ranked" not in response.server_events[0]
    assert _trace_rows(service) == []