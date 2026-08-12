"""H8 session summary personalization tests (Hybrid Integration Plan
section 22).

H8 renders a verified session summary onto the sync response: facts are
derived deterministically from committed session state (question attempts,
skills practiced, misconception evidence, interventions shown, validated
episodes, transfer outcomes, review-due skills) inside the advisory-lock
transaction; the model is called once per accepted SESSION_COMPLETED, after
commit and outside the lock; the proposal is fail-closed verified
(evidence refs grounded, no prohibited claims, no unsafe formatting, no
ungrounded numbers, at most two sentences); every failure leaves the
response's ``personalized_summary`` empty so the PWA keeps its
deterministic prose (offline included).

These tests prove:

- the task is off by default and requires the master flag;
- a verified, fully grounded summary is attached once per accepted
  SESSION_COMPLETED;
- ungrounded refs, invented numbers, prohibited claims, unsafe formatting,
  and over-long proposals are rejected (field stays empty);
- provider unavailability degrades to the deterministic surface;
- an empty session produces no model call;
- answer batches never produce a summary;
- duplicate completion syncs stay idempotent (single model call);
- agent-level ``verify_summary`` grounding checks fail closed.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from app.agent.hybrid import (
    build_summary_prompt,
    parse_summary_proposal,
    run_shadow_summary,
    verify_summary,
)
from app.agent.hybrid_contracts import (
    SessionSummaryContext,
    SummaryFact,
    SummaryProposal,
)
from app.agent.llm_client import LLMClient
from app.infrastructure import pg
from app.sync.protocol import SyncEventEnvelope, SyncRequest
from app.sync.service import SyncService

PACK_VERSION = "0.1.0"

STUDENT_ID = "student_01"
DEVICE_A = "device_a"
SESSION_ID = "session_01"


def _integrity(event_type: str, payload: dict) -> str:
    digest = hashlib.sha256()
    digest.update(event_type.encode("utf-8"))
    digest.update(b"\x00")
    digest.update(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    return f"sha256:{digest.hexdigest()}"


def _completion_envelope(
    *, event_id: str, device_sequence: int = 1, session_id: str = SESSION_ID
) -> dict:
    payload = {"student_id": STUDENT_ID}
    return {
        "event_id": event_id,
        "student_id": STUDENT_ID,
        "session_id": session_id,
        "session_branch_id": "branch_" + DEVICE_A,
        "device_id": DEVICE_A,
        "device_sequence": device_sequence,
        "event_type": "SESSION_COMPLETED",
        "payload": payload,
        "content_pack_version": PACK_VERSION,
        "question_id": None,
        "question_version": None,
        "policy_version": "offline-policy-v1",
        "depends_on_event_ids": [],
        "device_occurred_at": "2026-08-07T17:00:00+08:00",
        "integrity_hash": _integrity("SESSION_COMPLETED", payload),
    }


def _answer_envelope(*, event_id: str, device_sequence: int = 1) -> dict:
    payload = {
        "question_id": "sync.linear.001",
        "question_version": 1,
        "selected_choice_id": "B",
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
        "question_id": "sync.linear.001",
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
    payload = {"name": "Test Student", "daily_minutes": 20, "target_score": 1200}
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
                f"evt_seed_{STUDENT_ID}", STUDENT_ID, "", "STUDENT_CREATED",
                json.dumps(payload), "policy-0.1.0", None, now, now, None, None,
                "online",
                compute_integrity_hash("STUDENT_CREATED", payload),
            ),
        )


def _seed_session_facts(service: SyncService) -> None:
    """A realistic completed session: 3 attempts across 1 skill, 2 sign-error
    evidence records, one worked-example intervention, one validated transfer
    episode, and linear_equations due for review. Produces facts:
    stat:attempts (3), stat:skills (1), stat:misconception:linear_equations
    (2), stat:interventions, stat:episodes (1), stat:transfer (1),
    stat:review:linear_equations."""
    now = "2026-08-07T16:30:00+08:00"
    with pg.transaction(service.connection):
        for index in range(3):
            service.connection.execute(
                """
                INSERT INTO answer_attempts (
                    attempt_id, tenant_id, event_id, student_id, session_id,
                    content_id, version, sequence, selected_choice_id, correct,
                    hint_level, weight, validity, occurred_at
                ) VALUES (
                    %s, current_setting('app.tenant_id'), %s, %s, %s,
                    'sync.linear.001', 1, %s, 'B', 0, 0, 1.0, 'valid', %s
                )
                """,
                (
                    f"att_seed_{index}", f"evt_seed_att_{index}", STUDENT_ID,
                    SESSION_ID, index + 1, now,
                ),
            )
        for index in range(2):
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
                    f"evid_seed_{index}", STUDENT_ID, SESSION_ID,
                    f"evt_seed_evid_{index}", "linear_equations",
                    "isolate_variables", "sign_error", "seed.item.alpha",
                    1, now,
                ),
            )
        service.connection.execute(
            """
            INSERT INTO agent_events (
                tenant_id, event_id, student_id, session_id, source_event_id,
                state_before, state_after, action, action_payload_json,
                reason_code, reason_text, policy_version, content_version,
                referenced_content_json, episode_ids_json, source, created_at
            ) VALUES (
                current_setting('app.tenant_id'), %s, %s, %s, %s,
                'ANSWER_EVALUATED', 'ANSWER_EVALUATED', 'SHOW_WORKED_EXAMPLE',
                '{}', 'REPEATED_MISCONCEPTION', 'A worked example was shown',
                'offline-policy-v1', NULL, '[]', '[]', 'offline', %s
            )
            """,
            ("evt_seed_agent_1", STUDENT_ID, SESSION_ID, "evt_seed_evid_0", now),
        )
        service.connection.execute(
            """
            INSERT INTO learning_episodes (
                episode_id, tenant_id, student_id, session_id, skill,
                misconception, intervention, outcome_json, effectiveness,
                evidence_event_ids_json, summary, confidence, status,
                created_at, updated_at
            ) VALUES (
                %s, current_setting('app.tenant_id'), %s, %s,
                'linear_equations', 'sign_error', 'SHOW_WORKED_EXAMPLE', %s,
                0.9, %s, %s, 0.8, 'validated', %s, %s
            )
            """,
            (
                "ep_seed_1", STUDENT_ID, SESSION_ID,
                json.dumps({"correct": True, "different_item": True}),
                json.dumps(["evt_seed_evid_0"]),
                "worked example fixed sign error on a different item",
                now, now,
            ),
        )
        presentation_payload = {
            "source_answer_event_id": "evt_seed_att_0",
            "content_id": "we_linear_001",
            "content_version": 1,
            "skill": "linear_equations",
            "misconception": "sign_error",
            "intervention": "SHOW_WORKED_EXAMPLE",
        }
        service.connection.execute(
            """
            INSERT INTO learning_events (
                tenant_id, event_id, student_id, session_id, event_type,
                payload_json, policy_version, content_version, occurred_at,
                received_at, device_id, device_sequence, origin, integrity_hash
            ) VALUES (
                current_setting('app.tenant_id'), %s, %s, %s,
                'WORKED_EXAMPLE_PRESENTED', %s, 'offline-policy-v1', %s,
                %s, %s, %s, %s, 'offline', %s
            )
            """,
            (
                "evt_seed_presented_1", STUDENT_ID, SESSION_ID,
                json.dumps(presentation_payload), "0.1.0", now, now,
                DEVICE_A, 3, _integrity("WORKED_EXAMPLE_PRESENTED", presentation_payload),
            ),
        )
        service.connection.execute(
            """
            INSERT INTO student_skill_states (
                tenant_id, student_id, skill, alpha, beta, mastery, confidence,
                evidence_count, correct_streak, incorrect_streak,
                last_practiced_at, review_due_at, projection_origin, updated_at
            ) VALUES (
                current_setting('app.tenant_id'), %s, 'linear_equations',
                2.0, 2.0, 0.35, 0.0, 3, 0, 0, %s, %s, 'live', %s
            )
            """,
            (STUDENT_ID, now, "2026-08-07T15:00:00+08:00", now),
        )


def _seed_session_attempt(service: SyncService, session_id: str = "session_02") -> None:
    with pg.transaction(service.connection):
        service.connection.execute(
            """
            INSERT INTO answer_attempts (
                attempt_id, tenant_id, event_id, student_id, session_id,
                content_id, version, sequence, selected_choice_id, correct,
                hint_level, weight, validity, occurred_at
            ) VALUES (
                'att_seed_second', current_setting('app.tenant_id'),
                'evt_seed_second', %s, %s, 'sync.linear.001', 1,
                1, 'B', 0, 0, 1.0, 'valid', '2026-08-07T16:30:00+08:00'
            )
            """,
            (STUDENT_ID, session_id),
        )


def _seed_second_session_attempt(service: SyncService) -> None:
    _seed_session_attempt(service)


class ChatTransport:
    """Replay transport: returns scripted content or fails as unavailable."""

    def __init__(self, contents: list[str] | None = None, *, fail: bool = False) -> None:
        self.contents: list[str] = list(contents or [])
        self.calls: list[tuple[str, dict, int]] = []
        self.fail = fail

    async def request(self, url: str, body: dict, timeout_ms: int) -> dict:
        self.calls.append((url, body, timeout_ms))
        if self.fail or not self.contents:
            from app.agent.llm_client import LLMUnavailableError

            raise LLMUnavailableError("provider unavailable")
        content = self.contents.pop(0)
        return {"choices": [{"message": {"role": "assistant", "content": content}}]}


def _fake_client(transport: ChatTransport) -> LLMClient:
    return LLMClient(api_key="nvapi-test", model="test/model", transport=transport)


def _summary_flags_on(monkeypatch) -> None:
    monkeypatch.setenv("BRIDGESAT_HYBRID_ENABLED", "1")
    monkeypatch.setenv("BRIDGESAT_HYBRID_SUMMARY_ENABLED", "1")


def _make_service(isolated_pg_database, client: LLMClient | None = None) -> SyncService:
    admin = pg.connect_admin(isolated_pg_database.admin_dsn)
    try:
        with pg.transaction(admin):
            admin.execute(
                """
                INSERT INTO content_items (
                    content_id, version, content_type, target_skill,
                    target_subskill, review_status, body
                ) VALUES (
                    'sync.linear.001', 1, 'question', 'linear_equations',
                    'isolate_variables', 'approved', '{}'
                )
                """
            )
    finally:
        pg.quiet_close(admin)
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


def _request(events: list[dict]) -> SyncRequest:
    return SyncRequest.model_validate(
        {
            "student_id": STUDENT_ID,
            "device_id": DEVICE_A,
            "events": events,
        }
    )


def _summary_json(*, summary_text: str, refs: list[str]) -> str:
    return json.dumps({"summary_text": summary_text, "evidence_refs": refs})


# ---------------------------------------------------------------------------
# Agent-level verify_summary fail-closed grounding
# ---------------------------------------------------------------------------


def _unit_context() -> SessionSummaryContext:
    return SessionSummaryContext(
        task="session_summary",
        session_summary_facts=[
            SummaryFact(ref="stat:attempts", phrase="3 questions attempted in this session"),
            SummaryFact(ref="stat:skills", phrase="1 skill practiced this session"),
            SummaryFact(
                ref="stat:misconception:linear_equations",
                phrase="sign_error evidence recorded 2 times on linear_equations (high confidence)",
            ),
            SummaryFact(
                ref="stat:episodes",
                phrase="1 validated learning strategy recorded this session",
            ),
            SummaryFact(
                ref="stat:review:linear_equations",
                phrase="linear_equations is due for review next session",
            ),
        ]
    )


def _grounded_proposal() -> SummaryProposal:
    return SummaryProposal(
        summary_text=(
            "3 questions were attempted and 1 validated learning strategy "
            "was recorded this session. linear_equations is due for review "
            "next session."
        ),
        evidence_refs=(
            "stat:attempts",
            "stat:episodes",
            "stat:review:linear_equations",
        ),
    )


def test_verify_summary_accepts_fully_grounded_proposal() -> None:
    outcome = verify_summary(_unit_context(), _grounded_proposal())
    assert outcome.accepted is True
    assert "summary_refs_grounded" in outcome.checks
    assert "no_prohibited_claims" in outcome.checks
    assert "numbers_grounded" in outcome.checks
    assert "sentence_bounded" in outcome.checks


def test_verify_summary_rejects_ungrounded_ref() -> None:
    proposal = _grounded_proposal().model_copy(
        update={"evidence_refs": ("stat:invented",)}
    )
    outcome = verify_summary(_unit_context(), proposal)
    assert outcome.accepted is False
    assert outcome.rejected_reason == "ungrounded_summary_ref"


def test_verify_summary_rejects_spelled_number_and_unsupported_qualitative_claim() -> None:
    proposal = SummaryProposal(
        summary_text="3 questions were attempted and you mastered every skill.",
        evidence_refs=("stat:attempts",),
    )

    outcome = verify_summary(_unit_context(), proposal)

    assert outcome.accepted is False
    assert outcome.rejected_reason == "unsupported_claim"


def test_verify_summary_rejects_ungrounded_spelled_number() -> None:
    proposal = SummaryProposal(
        summary_text="Five questions were attempted this session.",
        evidence_refs=("stat:attempts",),
    )

    outcome = verify_summary(_unit_context(), proposal)

    assert outcome.accepted is False
    assert outcome.rejected_reason == "ungrounded_number"


def test_verify_summary_numbers_must_come_from_cited_facts() -> None:
    context = SessionSummaryContext(
        task="session_summary",
        session_summary_facts=[
            SummaryFact(ref="stat:attempts", phrase="20 questions attempted"),
            SummaryFact(ref="stat:skills", phrase="1 skill practiced"),
        ],
    )
    proposal = SummaryProposal(
        summary_text="1 skill was practiced after 20 questions.",
        evidence_refs=("stat:attempts",),
    )

    outcome = verify_summary(context, proposal)

    assert outcome.accepted is False
    assert outcome.rejected_reason == "ungrounded_number"


def test_verify_summary_rejects_compound_number_not_in_cited_fact() -> None:
    context = SessionSummaryContext(
        task="session_summary",
        session_summary_facts=[
            SummaryFact(ref="stat:attempts", phrase="20 questions attempted"),
            SummaryFact(ref="stat:skills", phrase="1 skill practiced"),
        ],
    )
    proposal = SummaryProposal(
        summary_text="Twenty-one questions were attempted.",
        evidence_refs=("stat:attempts", "stat:skills"),
    )

    outcome = verify_summary(context, proposal)

    assert outcome.accepted is False
    assert outcome.rejected_reason == "ungrounded_number"


def test_verify_summary_rejects_signed_count() -> None:
    context = SessionSummaryContext(
        task="session_summary",
        session_summary_facts=[
            SummaryFact(ref="stat:attempts", phrase="20 questions attempted"),
        ],
    )
    proposal = SummaryProposal(
        summary_text="-20 questions were attempted.",
        evidence_refs=("stat:attempts",),
    )

    outcome = verify_summary(context, proposal)

    assert outcome.accepted is False
    assert outcome.rejected_reason == "ungrounded_number"


def test_verify_summary_rejects_unicode_signed_count() -> None:
    context = SessionSummaryContext(
        task="session_summary",
        session_summary_facts=[
            SummaryFact(ref="stat:attempts", phrase="20 questions attempted"),
        ],
    )
    proposal = SummaryProposal(
        summary_text="−20 questions were attempted.",
        evidence_refs=("stat:attempts",),
    )

    outcome = verify_summary(context, proposal)

    assert outcome.accepted is False
    assert outcome.rejected_reason == "ungrounded_number"


def test_verify_summary_rejects_fullwidth_signed_count() -> None:
    context = SessionSummaryContext(
        task="session_summary",
        session_summary_facts=[
            SummaryFact(ref="stat:attempts", phrase="20 questions attempted"),
        ],
    )
    proposal = SummaryProposal(
        summary_text="＋20 questions were attempted.",
        evidence_refs=("stat:attempts",),
    )

    outcome = verify_summary(context, proposal)

    assert outcome.accepted is False
    assert outcome.rejected_reason == "ungrounded_number"


@pytest.mark.parametrize("sign", ("﹣", "⁻", "➖", "−\u200b"))
def test_verify_summary_rejects_unicode_signed_count_variants(sign: str) -> None:
    context = SessionSummaryContext(
        task="session_summary",
        session_summary_facts=[
            SummaryFact(ref="stat:attempts", phrase="20 questions attempted"),
        ],
    )
    proposal = SummaryProposal(
        summary_text=f"{sign}20 questions were attempted.",
        evidence_refs=("stat:attempts",),
    )

    outcome = verify_summary(context, proposal)

    assert outcome.accepted is False
    assert outcome.rejected_reason == "ungrounded_number"


@pytest.mark.parametrize("numeric_symbol", ("²", "Ⅹ"))
def test_verify_summary_rejects_unicode_numeric_symbols(numeric_symbol: str) -> None:
    context = SessionSummaryContext(
        task="session_summary",
        session_summary_facts=[
            SummaryFact(ref="stat:attempts", phrase="20 questions attempted"),
        ],
    )
    proposal = SummaryProposal(
        summary_text=f"{numeric_symbol} questions were attempted.",
        evidence_refs=("stat:attempts",),
    )

    outcome = verify_summary(context, proposal)

    assert outcome.accepted is False
    assert outcome.rejected_reason == "ungrounded_number"


def test_verify_summary_does_not_recombine_numbers_across_facts() -> None:
    context = SessionSummaryContext(
        task="session_summary",
        session_summary_facts=[
            SummaryFact(ref="stat:attempts", phrase="20 questions attempted"),
            SummaryFact(ref="stat:skills", phrase="3 skills practiced"),
        ],
    )
    proposal = SummaryProposal(
        summary_text="20 skills were practiced and 3 questions were attempted.",
        evidence_refs=("stat:attempts", "stat:skills"),
    )

    outcome = verify_summary(context, proposal)

    assert outcome.accepted is False
    assert outcome.rejected_reason == "ungrounded_number"


def test_verify_summary_binds_number_subject_and_predicate_to_one_fact() -> None:
    context = SessionSummaryContext(
        task="session_summary",
        session_summary_facts=[
            SummaryFact(ref="stat:attempts", phrase="3 questions attempted"),
            SummaryFact(ref="stat:skills", phrase="1 skill practiced"),
        ],
    )
    proposal = SummaryProposal(
        summary_text="3 questions were practiced.",
        evidence_refs=("stat:attempts", "stat:skills"),
    )

    outcome = verify_summary(context, proposal)

    assert outcome.accepted is False
    assert outcome.rejected_reason == "ungrounded_number"


def test_verify_summary_rejects_qualitative_predicate_recombination() -> None:
    context = SessionSummaryContext(
        task="session_summary",
        session_summary_facts=[
            SummaryFact(ref="stat:attempts", phrase="20 questions attempted"),
            SummaryFact(ref="stat:skills", phrase="1 skill practiced"),
        ],
    )
    proposal = SummaryProposal(
        summary_text="Questions were practiced and skill was attempted.",
        evidence_refs=("stat:attempts", "stat:skills"),
    )

    outcome = verify_summary(context, proposal)

    assert outcome.accepted is False
    assert outcome.rejected_reason == "unsupported_claim"


def test_verify_summary_rejects_non_plain_text_unicode() -> None:
    proposal = _grounded_proposal().model_copy(
        update={"summary_text": "3 questions were attempted this session. 🎯"}
    )

    outcome = verify_summary(_unit_context(), proposal)

    assert outcome.accepted is False
    assert outcome.rejected_reason == "unsafe_formatting"


def test_verify_summary_rejects_empty_refs() -> None:
    proposal = _grounded_proposal().model_copy(update={"evidence_refs": ()})
    outcome = verify_summary(_unit_context(), proposal)
    assert outcome.accepted is False
    assert outcome.rejected_reason == "ungrounded_summary_ref"


def test_verify_summary_rejects_prohibited_claim() -> None:
    proposal = _grounded_proposal().model_copy(
        update={
            "summary_text": (
                "3 questions were attempted; this practice will guarantee a "
                "higher score."
            )
        }
    )
    outcome = verify_summary(_unit_context(), proposal)
    assert outcome.accepted is False
    assert outcome.rejected_reason == "prohibited_claim"


def test_verify_summary_rejects_unsafe_formatting() -> None:
    proposal = _grounded_proposal().model_copy(
        update={"summary_text": "3 questions attempted.\n\n**Bold claim**."}
    )
    outcome = verify_summary(_unit_context(), proposal)
    assert outcome.accepted is False
    assert outcome.rejected_reason == "unsafe_formatting"


def test_verify_summary_rejects_ungrounded_number() -> None:
    proposal = _grounded_proposal().model_copy(
        update={
            "summary_text": (
                "5 validated learning strategies were recorded this session."
            )
        }
    )
    outcome = verify_summary(_unit_context(), proposal)
    assert outcome.accepted is False
    assert outcome.rejected_reason == "ungrounded_number"


def test_verify_summary_rejects_too_many_sentences() -> None:
    proposal = _grounded_proposal().model_copy(
        update={
            "summary_text": (
                "3 questions were attempted this session. 1 validated "
                "learning strategy was recorded. linear_equations is due for "
                "review next session."
            )
        }
    )
    outcome = verify_summary(_unit_context(), proposal)
    assert outcome.accepted is False
    assert outcome.rejected_reason == "too_many_sentences"


def test_verify_summary_rejects_unsupported_qualitative_claim() -> None:
    proposal = _grounded_proposal().model_copy(
        update={
            "summary_text": "You mastered every skill and are ready for the next level.",
            "evidence_refs": ("stat:attempts",),
        }
    )
    outcome = verify_summary(_unit_context(), proposal)
    assert outcome.accepted is False
    assert outcome.rejected_reason == "unsupported_claim"


def test_verify_summary_rejects_claim_without_its_fact_ref() -> None:
    proposal = _grounded_proposal().model_copy(
        update={
                "summary_text": (
                    "3 questions were attempted and a validated learning "
                    "strategy was recorded this session."
                ),
            "evidence_refs": ("stat:attempts",),
        }
    )
    outcome = verify_summary(_unit_context(), proposal)
    assert outcome.accepted is False
    assert outcome.rejected_reason == "unsupported_claim"


def test_build_summary_prompt_embeds_facts_and_static_rules() -> None:
    prompt = build_summary_prompt(_unit_context())
    assert "session_summary_facts" in prompt
    assert "never invent numbers" in prompt
    assert "Respond ONLY with JSON" in prompt


def test_parse_summary_proposal_handles_fenced_json() -> None:
    proposal = parse_summary_proposal(
        "```json\n" + _summary_json(summary_text="ok", refs=["stat:attempts"]) + "\n```"
    )
    assert proposal.summary_text == "ok"
    assert proposal.evidence_refs == ("stat:attempts",)


def test_run_shadow_summary_returns_none_when_gate_closed(monkeypatch) -> None:
    monkeypatch.delenv("BRIDGESAT_HYBRID_ENABLED", raising=False)
    transport = ChatTransport(contents=[_summary_json(summary_text="x", refs=["stat:attempts"])])
    client = _fake_client(transport)
    assert run_shadow_summary(_unit_context(), client) is None
    assert transport.calls == []


def test_run_shadow_summary_honors_remaining_timeout(monkeypatch) -> None:
    _summary_flags_on(monkeypatch)
    transport = ChatTransport(
        contents=[_summary_json(summary_text="ok", refs=["stat:attempts"])]
    )
    client = _fake_client(transport)

    assert run_shadow_summary(_unit_context(), client, timeout_ms=17) is not None
    assert transport.calls[0][2] == 17


# ---------------------------------------------------------------------------
# Sync path: off by default, gated, attached once per accepted completion
# ---------------------------------------------------------------------------


def test_summary_off_by_default_no_model_call(isolated_pg_database, monkeypatch) -> None:
    monkeypatch.setenv("BRIDGESAT_HYBRID_ENABLED", "1")
    transport = ChatTransport(contents=[_summary_json(summary_text="x", refs=["stat:attempts"])])
    service = _make_service(isolated_pg_database, client=_fake_client(transport))
    _seed_session_facts(service)

    response = service.process_batch(
        _request([_completion_envelope(event_id="evt_done_1")])
    )

    assert response.accepted_event_ids == ["evt_done_1"]
    assert response.personalized_summary is None
    assert transport.calls == []


def test_summary_context_caps_many_fact_rows(isolated_pg_database, monkeypatch) -> None:
    _summary_flags_on(monkeypatch)
    service = _make_service(isolated_pg_database)
    _seed_session_facts(service)
    with pg.transaction(service.connection):
        for index in range(20):
            service.connection.execute(
                """
                INSERT INTO misconception_evidence (
                    tenant_id, evidence_id, student_id, session_id, event_id, skill,
                    subskill, misconception, source_label, confidence_label, state,
                    item_id, item_version, observed_at
                ) VALUES (
                    current_setting('app.tenant_id'), %s, %s, %s, %s, %s, %s,
                    %s, 'offline_distractor', 'medium', 'confirmed_offline',
                    %s, 1, %s
                )
                """,
                (
                    f"evid_many_{index}", STUDENT_ID, SESSION_ID,
                    f"evt_many_{index}", f"skill_{index}", "subskill",
                    f"misconception_{index}", f"item_{index}", "2026-08-07T16:30:00+08:00",
                ),
            )
    envelope = SyncEventEnvelope.model_validate(
        _completion_envelope(event_id="evt_done_context")
    )

    with pg.transaction(service.connection):
        context = service._build_session_summary_context(envelope)

    assert context is not None
    assert len(context.session_summary_facts) <= 16


def test_summary_context_keeps_distinct_misconception_facts(
    isolated_pg_database, monkeypatch
) -> None:
    _summary_flags_on(monkeypatch)
    service = _make_service(isolated_pg_database)
    _seed_session_facts(service)
    with pg.transaction(service.connection):
        service.connection.execute(
            """
            INSERT INTO misconception_evidence (
                tenant_id, evidence_id, student_id, session_id, event_id, skill,
                subskill, misconception, source_label, confidence_label, state,
                item_id, item_version, observed_at
            ) VALUES (
                current_setting('app.tenant_id'), 'evid_distinct', %s, %s,
                'evt_distinct', 'linear_equations', 'isolate_variables',
                'coefficient_error', 'offline_distractor', 'medium',
                'confirmed_offline', 'sync.linear.001', 1,
                '2026-08-07T16:30:00+00:00'
            )
            """,
            (STUDENT_ID, SESSION_ID),
        )

    with pg.transaction(service.connection):
        context = service._build_session_summary_context(
            SyncEventEnvelope.model_validate(_completion_envelope(event_id="evt_done_facts"))
        )

    refs = {
        fact.ref
        for fact in context.session_summary_facts
        if fact.ref.startswith("stat:misconception:")
    }
    assert refs == {
        "stat:misconception:linear_equations:coefficient_error:medium",
        "stat:misconception:linear_equations:sign_error:high",
    }


def test_summary_enrichment_failure_does_not_reject_completion(
    isolated_pg_database, monkeypatch
) -> None:
    _summary_flags_on(monkeypatch)
    service = _make_service(isolated_pg_database)

    def fail_context(_envelope):
        raise RuntimeError("summary context failure")

    monkeypatch.setattr(service, "_build_session_summary_context", fail_context)
    response = service.process_batch(
        _request([_completion_envelope(event_id="evt_done_failure")])
    )

    assert response.accepted_event_ids == ["evt_done_failure"]
    assert response.personalized_summary is None
    assert response.personalized_summaries == []


def test_summary_database_failure_does_not_abort_completion_transaction(
    isolated_pg_database, monkeypatch
) -> None:
    _summary_flags_on(monkeypatch)
    service = _make_service(isolated_pg_database)

    def fail_context(_envelope):
        service.connection.execute("SELECT * FROM missing_summary_table")

    monkeypatch.setattr(service, "_build_session_summary_context", fail_context)

    response = service.process_batch(
        _request([_completion_envelope(event_id="evt_done_db_failure")])
    )

    assert response.accepted_event_ids == ["evt_done_db_failure"]
    assert response.personalized_summaries == []


def test_summary_context_is_captured_while_completion_lock_is_held(
    isolated_pg_database, monkeypatch
) -> None:
    _summary_flags_on(monkeypatch)
    summary_text = "3 questions were attempted this session."
    transport = ChatTransport(
        contents=[_summary_json(summary_text=summary_text, refs=["stat:attempts"])]
    )
    service = _make_service(isolated_pg_database, client=_fake_client(transport))
    _seed_session_facts(service)
    lock_probe_results: list[bool] = []
    original_build = service._build_session_summary_context

    def build_with_lock_probe(envelope):
        probe = isolated_pg_database.connect_app()
        try:
            probe.execute(
                "SELECT set_config('app.tenant_id', %s, false)",
                (isolated_pg_database.tenant_id,),
            )
            lock_probe_results.append(
                probe.execute(
                    """
                    SELECT pg_try_advisory_lock(
                        hashtextextended(%s, 0)
                    ) AS acquired
                    """,
                    ("bridgesat:memory:student:" + STUDENT_ID,),
                ).fetchone()["acquired"]
            )
            if lock_probe_results[-1]:
                probe.execute(
                    "SELECT pg_advisory_unlock(hashtextextended(%s, 0))",
                    ("bridgesat:memory:student:" + STUDENT_ID,),
                )
            probe.commit()
        finally:
            pg.quiet_close(probe)
        return original_build(envelope)

    monkeypatch.setattr(service, "_build_session_summary_context", build_with_lock_probe)

    response = service.process_batch(
        _request([_completion_envelope(event_id="evt_done_lock_probe")])
    )

    assert response.accepted_event_ids == ["evt_done_lock_probe"]
    assert lock_probe_results == [False]


def test_long_session_id_summary_failure_does_not_reject_completion(
    isolated_pg_database, monkeypatch
) -> None:
    _summary_flags_on(monkeypatch)
    long_session_id = "s" * 129
    transport = ChatTransport(
        contents=[
            _summary_json(
                summary_text="1 question was attempted in this session.",
                refs=["stat:attempts"],
            )
        ]
    )
    service = _make_service(isolated_pg_database, client=_fake_client(transport))
    _seed_session_attempt(service, long_session_id)

    response = service.process_batch(
        _request(
            [
                _completion_envelope(
                    event_id="evt_done_long_session_id",
                    session_id=long_session_id,
                )
            ]
        )
    )

    assert response.accepted_event_ids == ["evt_done_long_session_id"]
    assert response.personalized_summary is None
    assert response.personalized_summaries == []
    assert len(transport.calls) == 1


def test_summary_requires_master_flag(isolated_pg_database, monkeypatch) -> None:
    monkeypatch.setenv("BRIDGESAT_HYBRID_ENABLED", "0")
    monkeypatch.setenv("BRIDGESAT_HYBRID_SUMMARY_ENABLED", "1")
    transport = ChatTransport(contents=[_summary_json(summary_text="x", refs=["stat:attempts"])])
    service = _make_service(isolated_pg_database, client=_fake_client(transport))
    _seed_session_facts(service)

    response = service.process_batch(
        _request([_completion_envelope(event_id="evt_done_1")])
    )

    assert response.personalized_summary is None
    assert transport.calls == []


def test_verified_summary_attached_once_on_session_completed(
    isolated_pg_database, monkeypatch
) -> None:
    _summary_flags_on(monkeypatch)
    summary_text = (
        "3 questions were attempted across 1 skill this session, and 1 "
        "validated learning strategy was recorded. linear_equations is due "
        "for review next session."
    )
    transport = ChatTransport(
        contents=[
            _summary_json(
                summary_text=summary_text,
                refs=[
                    "stat:attempts",
                    "stat:skills",
                    "stat:episodes",
                    "stat:review:linear_equations",
                ],
            )
        ]
    )
    service = _make_service(isolated_pg_database, client=_fake_client(transport))
    _seed_session_facts(service)

    response = service.process_batch(
        _request([_completion_envelope(event_id="evt_done_1")])
    )

    assert response.accepted_event_ids == ["evt_done_1"]
    assert response.personalized_summary == summary_text
    assert response.personalized_summaries[0].source_event_id == "evt_done_1"
    assert response.personalized_summaries[0].session_id == SESSION_ID
    assert len(transport.calls) == 1
    url, body, _timeout = transport.calls[0]
    prompt = body["messages"][0]["content"]
    assert "session_summary_facts" in prompt
    assert "stat:attempts" in prompt
    assert "stat:interventions" in prompt
    assert "stat:transfer" in prompt
    assert "stat:sync" in prompt


def test_duplicate_completion_is_idempotent_single_call(
    isolated_pg_database, monkeypatch
) -> None:
    _summary_flags_on(monkeypatch)
    transport = ChatTransport(
        contents=[
            _summary_json(
                summary_text=(
                    "3 questions were attempted this session, and 1 "
                    "validated learning strategy was recorded."
                ),
                refs=["stat:attempts", "stat:episodes"],
            )
        ]
    )
    service = _make_service(isolated_pg_database, client=_fake_client(transport))
    _seed_session_facts(service)
    request = _request([_completion_envelope(event_id="evt_done_1")])

    first = service.process_batch(request)
    second = service.process_batch(request)

    assert first.personalized_summary is not None
    assert second.accepted_event_ids == []
    assert second.personalized_summary is None
    assert len(transport.calls) == 1


def test_distinct_completion_for_completed_session_does_not_resummarize(
    isolated_pg_database, monkeypatch
) -> None:
    _summary_flags_on(monkeypatch)
    transport = ChatTransport(
        contents=[
            _summary_json(
                summary_text="3 questions were attempted in this session.",
                refs=["stat:attempts"],
            )
        ]
    )
    service = _make_service(isolated_pg_database, client=_fake_client(transport))
    _seed_session_facts(service)

    first = service.process_batch(
        _request([_completion_envelope(event_id="evt_done_first", device_sequence=1)])
    )
    second = service.process_batch(
        _request([_completion_envelope(event_id="evt_done_second", device_sequence=2)])
    )

    assert first.personalized_summary is not None
    assert second.accepted_event_ids == ["evt_done_second"]
    assert second.personalized_summaries == []
    assert len(transport.calls) == 1


def test_batched_completed_sessions_keep_summary_provenance(
    isolated_pg_database, monkeypatch
) -> None:
    _summary_flags_on(monkeypatch)
    transport = ChatTransport(
        contents=[
            _summary_json(
                summary_text="3 questions were attempted in this session.",
                refs=["stat:attempts"],
            ),
            _summary_json(
                summary_text="1 question was attempted in this session.",
                refs=["stat:attempts"],
            ),
        ]
    )
    service = _make_service(isolated_pg_database, client=_fake_client(transport))
    _seed_session_facts(service)
    _seed_second_session_attempt(service)

    response = service.process_batch(
        _request(
            [
                _completion_envelope(event_id="evt_done_1", device_sequence=1),
                _completion_envelope(
                    event_id="evt_done_2", device_sequence=2, session_id="session_02"
                ),
            ]
        )
    )

    assert response.personalized_summary is None
    assert [entry.source_event_id for entry in response.personalized_summaries] == [
        "evt_done_1",
        "evt_done_2",
    ]
    assert [entry.session_id for entry in response.personalized_summaries] == [
        SESSION_ID,
        "session_02",
    ]
    assert len(transport.calls) == 2


def test_summary_provider_calls_are_capped_per_batch(
    isolated_pg_database, monkeypatch
) -> None:
    _summary_flags_on(monkeypatch)
    transport = ChatTransport(
        contents=[
            _summary_json(
                summary_text="1 question was attempted in this session.",
                refs=["stat:attempts"],
            )
            for _ in range(9)
        ]
    )
    service = _make_service(isolated_pg_database, client=_fake_client(transport))
    with pg.transaction(service.connection):
        for index in range(9):
            session_id = f"batch_session_{index}"
            service.connection.execute(
                """
                INSERT INTO answer_attempts (
                    attempt_id, tenant_id, event_id, student_id, session_id,
                    content_id, version, sequence, selected_choice_id, correct,
                    hint_level, weight, validity, occurred_at
                ) VALUES (
                    %s, current_setting('app.tenant_id'), %s, %s, %s,
                    'sync.linear.001', 1, 1, 'B', 0, 0, 1.0, 'valid',
                    '2026-08-07T16:30:00+00:00'
                )
                """,
                (
                    f"att_batch_{index}",
                    f"evt_batch_attempt_{index}",
                    STUDENT_ID,
                    session_id,
                ),
            )

    response = service.process_batch(
        _request(
            [
                _completion_envelope(
                    event_id=f"evt_batch_done_{index}",
                    device_sequence=index + 1,
                    session_id=f"batch_session_{index}",
                )
                for index in range(9)
            ]
        )
    )

    assert len(response.accepted_event_ids) == 9
    assert len(response.personalized_summaries) == 8
    assert len(transport.calls) == 8


def test_empty_completion_contexts_do_not_consume_summary_cap(
    isolated_pg_database, monkeypatch
) -> None:
    _summary_flags_on(monkeypatch)
    transport = ChatTransport(
        contents=[
            _summary_json(
                summary_text="1 question was attempted in this session.",
                refs=["stat:attempts"],
            )
        ]
    )
    service = _make_service(isolated_pg_database, client=_fake_client(transport))
    with pg.transaction(service.connection):
        service.connection.execute(
            """
            INSERT INTO answer_attempts (
                attempt_id, tenant_id, event_id, student_id, session_id,
                content_id, version, sequence, selected_choice_id, correct,
                hint_level, weight, validity, occurred_at
            ) VALUES (
                'att_cap_fact', current_setting('app.tenant_id'),
                'evt_cap_fact', %s, 'batch_fact_session', 'sync.linear.001',
                1, 1, 'B', 0, 0, 1.0, 'valid', '2026-08-07T16:30:00+00:00'
            )
            """,
            (STUDENT_ID,),
        )

    response = service.process_batch(
        _request(
            [
                _completion_envelope(
                    event_id=f"evt_empty_done_{index}",
                    device_sequence=index + 1,
                    session_id=f"empty_batch_session_{index}",
                )
                for index in range(8)
            ]
            + [
                _completion_envelope(
                    event_id="evt_fact_done",
                    device_sequence=9,
                    session_id="batch_fact_session",
                )
            ]
        )
    )

    assert len(response.accepted_event_ids) == 9
    assert len(response.personalized_summaries) == 1
    assert response.personalized_summaries[0].source_event_id == "evt_fact_done"
    assert len(transport.calls) == 1


# ---------------------------------------------------------------------------
# Failures degrade to the deterministic surface
# ---------------------------------------------------------------------------


def test_invented_number_rejected_field_stays_empty(
    isolated_pg_database, monkeypatch
) -> None:
    _summary_flags_on(monkeypatch)
    transport = ChatTransport(
        contents=[
            _summary_json(
                summary_text="5 validated learning strategies were recorded this session.",
                refs=["stat:episodes"],
            )
        ]
    )
    service = _make_service(isolated_pg_database, client=_fake_client(transport))
    _seed_session_facts(service)

    response = service.process_batch(
        _request([_completion_envelope(event_id="evt_done_1")])
    )

    assert response.accepted_event_ids == ["evt_done_1"]
    assert response.personalized_summary is None


def test_prohibited_claim_rejected_field_stays_empty(
    isolated_pg_database, monkeypatch
) -> None:
    _summary_flags_on(monkeypatch)
    transport = ChatTransport(
        contents=[
            _summary_json(
                summary_text=(
                    "3 questions were attempted; this practice guarantees a "
                    "higher score."
                ),
                refs=["stat:attempts"],
            )
        ]
    )
    service = _make_service(isolated_pg_database, client=_fake_client(transport))
    _seed_session_facts(service)

    response = service.process_batch(
        _request([_completion_envelope(event_id="evt_done_1")])
    )

    assert response.personalized_summary is None


def test_model_unavailable_field_stays_empty(isolated_pg_database, monkeypatch) -> None:
    _summary_flags_on(monkeypatch)
    transport = ChatTransport(fail=True)
    service = _make_service(isolated_pg_database, client=_fake_client(transport))
    _seed_session_facts(service)

    response = service.process_batch(
        _request([_completion_envelope(event_id="evt_done_1")])
    )

    assert response.accepted_event_ids == ["evt_done_1"]
    assert response.personalized_summary is None
    assert len(transport.calls) == 1


def test_empty_session_produces_no_model_call(isolated_pg_database, monkeypatch) -> None:
    _summary_flags_on(monkeypatch)
    transport = ChatTransport(contents=["{}"])
    service = _make_service(isolated_pg_database, client=_fake_client(transport))

    response = service.process_batch(
        _request([_completion_envelope(event_id="evt_done_1")])
    )

    assert response.accepted_event_ids == ["evt_done_1"]
    assert response.personalized_summary is None
    assert transport.calls == []


def test_answer_batch_never_produces_summary(isolated_pg_database, monkeypatch) -> None:
    _summary_flags_on(monkeypatch)
    transport = ChatTransport(contents=["{}"])
    service = _make_service(isolated_pg_database, client=_fake_client(transport))
    _seed_session_facts(service)

    response = service.process_batch(
        _request([_answer_envelope(event_id="evt_01", device_sequence=1)])
    )

    assert response.accepted_event_ids == ["evt_01"]
    assert response.personalized_summary is None
    assert transport.calls == []


def test_answer_then_completion_summary_covers_whole_session(
    isolated_pg_database, monkeypatch
) -> None:
    _summary_flags_on(monkeypatch)
    transport = ChatTransport(
        contents=[
            _summary_json(
                    summary_text=(
                        "4 questions were attempted and 1 validated learning "
                        "strategy was recorded this session."
                    ),
                refs=["stat:attempts", "stat:episodes"],
            )
        ]
    )
    service = _make_service(isolated_pg_database, client=_fake_client(transport))
    _seed_session_facts(service)

    response = service.process_batch(
        _request(
            [
                _answer_envelope(event_id="evt_01", device_sequence=1),
                _completion_envelope(event_id="evt_done_1", device_sequence=2),
            ]
        )
    )

    assert response.accepted_event_ids == ["evt_01", "evt_done_1"]
    assert response.personalized_summary is not None
    assert len(transport.calls) == 1
