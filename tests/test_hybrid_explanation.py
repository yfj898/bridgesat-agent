"""H5 grounded personalized explanation tests (Hybrid Integration Plan H5).

The explanation task writes ONE optional student-safe sentence behind the
existing deterministic "Why this recommendation?" surface. These tests prove
the gate stays deterministic unless configured/healthy/enabled, the verifier
fails closed (ungrounded refs, protected-span rewrites, prohibited claims,
ungrounded numbers, unbounded sentences), and the gateway never raises and
never changes the executed action.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.agent.hybrid import (
    MODE_DETERMINISTIC,
    MODE_HYBRID,
    HybridAvailability,
    HybridTask,
    build_explanation_prompt,
    explanation_gate,
    parse_explanation_proposal,
    run_shadow_explanation,
    task_settings,
    verify_explanation,
)
from app.agent.hybrid_contracts import (
    ExplanationContext,
    ExplanationFact,
    ExplanationProposal,
)
from app.agent.llm_client import LLMClient, LLMUnavailableError
from app.domain.memory import BoundedAction


def _context(**overrides) -> ExplanationContext:
    values = dict(
        task="explanation",
        skill="linear_equations",
        subskill="isolate_variables",
        fallback_action=BoundedAction.SHOW_WORKED_EXAMPLE,
        reason_code="REPEATED_MISCONCEPTION",
        reason_text=(
            "Repeated errors map to the same misconception, so a worked "
            "example isolates the error pattern before more practice."
        ),
        lesson_title="Sign error worked example",
        misconception="sign_error",
        misconception_evidence_count=2,
        misconception_confidence="high",
        learner_summary="2 wrong answers in a row on linear_equations; "
        "2 recorded misconception errors this session.",
        facts=(
            ExplanationFact(
                ref="stat:misconception",
                phrase="2 recorded sign_error errors in this session",
            ),
            ExplanationFact(
                ref="stat:consecutive_errors",
                phrase="2 consecutive wrong answers on linear_equations",
            ),
            ExplanationFact(ref="stat:mastery", phrase="mastery 0.50"),
        ),
        protected_spans=(
            "Repeated errors map to the same misconception, so a worked "
            "example isolates the error pattern before more practice.",
            "Sign error worked example",
        ),
    )
    values.update(overrides)
    return ExplanationContext(**values)


def _proposal(*, refs=("stat:misconception",), explanation=None, **overrides) -> ExplanationProposal:
    values = dict(
        student_explanation=(
            explanation
            or "Because two sign error mistakes were recorded in this "
            "session, a worked example isolates the pattern and builds "
            "mastery."
        ),
        emphasis="process",
        evidence_refs=refs,
    )
    values.update(overrides)
    return ExplanationProposal(**values)


class SequenceTransport:
    def __init__(self, contents: list[str]) -> None:
        self.contents = list(contents)
        self.calls: list[tuple[str, dict, int]] = []

    async def request(self, url: str, body: dict, timeout_ms: int) -> dict:
        self.calls.append((url, body, timeout_ms))
        content = self.contents.pop(0)
        return {"choices": [{"message": {"role": "assistant", "content": content}}]}


class FailTransport:
    def __init__(self, exc: Exception | None = None) -> None:
        self.exc = exc or LLMUnavailableError("unavailable")

    async def request(self, url: str, body: dict, timeout_ms: int) -> dict:
        raise self.exc


def _client(transport) -> LLMClient:
    return LLMClient(api_key="nvapi-test", model="test/model", transport=transport)


# ---------------------------------------------------------------------------
# Contract bounds
# ---------------------------------------------------------------------------


def test_explanation_proposal_contract_bounds() -> None:
    with pytest.raises(ValidationError):
        ExplanationProposal(student_explanation="", emphasis="process", evidence_refs=("r",))
    with pytest.raises(ValidationError):
        ExplanationProposal(
            student_explanation="ok", emphasis="not_an_emphasis", evidence_refs=("r",)
        )
    with pytest.raises(ValidationError):
        ExplanationProposal(
            student_explanation="ok", emphasis="process", evidence_refs=()
        )
    with pytest.raises(ValidationError):
        ExplanationProposal(
            student_explanation="x" * 321, emphasis="process", evidence_refs=("r",)
        )
    proposal = ExplanationProposal(
        student_explanation="ok", emphasis="sign", evidence_refs=("episode:abc",)
    )
    assert proposal.emphasis == "sign"


def test_explanation_context_bounds_and_extra_forbid() -> None:
    with pytest.raises(ValidationError):
        ExplanationContext(**{**_context().model_dump(), "task": "other"})
    with pytest.raises(ValidationError):
        ExplanationContext(**{**_context().model_dump(), "extra_field": 1})


# ---------------------------------------------------------------------------
# Prompt and parsing
# ---------------------------------------------------------------------------


def test_build_explanation_prompt_contains_only_structured_context() -> None:
    context = _context()
    prompt = build_explanation_prompt(context)
    assert "ALREADY CHOSEN" in prompt
    assert "No guarantees" in prompt
    assert context.model_dump_json() in prompt
    assert "student_01" not in prompt
    assert "nvapi" not in prompt


def test_parse_explanation_proposal_accepts_plain_and_fenced_json() -> None:
    payload = json.dumps(
        {
            "student_explanation": "Practice again to confirm the pattern.",
            "emphasis": "review",
            "evidence_refs": ["stat:consecutive_errors"],
        }
    )
    assert parse_explanation_proposal(payload).emphasis == "review"
    assert parse_explanation_proposal(f"```json\n{payload}\n```").emphasis == "review"
    assert parse_explanation_proposal(f"Sure! {payload} hope that helps").emphasis == "review"


def test_parse_explanation_proposal_rejects_garbage() -> None:
    for text in ("", "no json here", "[]", '{"student_explanation": 5}', '{"x": 1}'):
        with pytest.raises((ValueError, json.JSONDecodeError, ValidationError)):
            parse_explanation_proposal(text)


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------


def test_explanation_gate_stays_deterministic_when_not_configured(monkeypatch) -> None:
    monkeypatch.setenv("BRIDGESAT_HYBRID_ENABLED", "1")
    monkeypatch.setenv("BRIDGESAT_HYBRID_EXPLANATION_ENABLED", "1")
    gate = explanation_gate(HybridAvailability(configured=False))
    assert gate.mode == MODE_DETERMINISTIC
    assert gate.reasons == ("not_configured",)


def test_explanation_gate_requires_task_flag(monkeypatch) -> None:
    monkeypatch.setenv("BRIDGESAT_HYBRID_ENABLED", "1")
    monkeypatch.setenv("BRIDGESAT_HYBRID_EXPLANATION_ENABLED", "0")
    settings = task_settings(HybridTask.EXPLANATION)
    assert not settings.enabled
    gate = explanation_gate(HybridAvailability(configured=True), settings=settings)
    assert gate.mode == MODE_DETERMINISTIC
    assert gate.reasons == ("task_disabled",)


def test_explanation_gate_requires_master_flag(monkeypatch) -> None:
    monkeypatch.setenv("BRIDGESAT_HYBRID_ENABLED", "0")
    monkeypatch.setenv("BRIDGESAT_HYBRID_EXPLANATION_ENABLED", "1")
    gate = explanation_gate(HybridAvailability(configured=True))
    assert gate.mode == MODE_DETERMINISTIC
    assert gate.reasons == ("not_configured",)


def test_explanation_gate_honors_circuit_and_budget(monkeypatch) -> None:
    monkeypatch.setenv("BRIDGESAT_HYBRID_ENABLED", "1")
    monkeypatch.setenv("BRIDGESAT_HYBRID_EXPLANATION_ENABLED", "1")
    gate = explanation_gate(HybridAvailability(configured=True, circuit_open=True))
    assert gate.mode == MODE_DETERMINISTIC
    assert gate.reasons == ("circuit_open",)
    gate = explanation_gate(HybridAvailability(configured=True, budget_exhausted=True))
    assert gate.mode == MODE_DETERMINISTIC
    assert gate.reasons == ("budget_exhausted",)


def test_explanation_gate_hybrid_when_healthy_and_enabled(monkeypatch) -> None:
    monkeypatch.setenv("BRIDGESAT_HYBRID_ENABLED", "1")
    monkeypatch.setenv("BRIDGESAT_HYBRID_EXPLANATION_ENABLED", "1")
    gate = explanation_gate(HybridAvailability(configured=True))
    assert gate.mode == MODE_HYBRID


def test_explanation_gate_offline_is_deterministic(monkeypatch) -> None:
    monkeypatch.setenv("BRIDGESAT_HYBRID_ENABLED", "1")
    monkeypatch.setenv("BRIDGESAT_HYBRID_EXPLANATION_ENABLED", "1")
    gate = explanation_gate(HybridAvailability(configured=True), offline=True)
    assert gate.mode == MODE_DETERMINISTIC
    assert gate.reasons == ("offline",)


# ---------------------------------------------------------------------------
# Verifier
# ---------------------------------------------------------------------------


def test_verify_gold_explanation_accepted() -> None:
    outcome = verify_explanation(_context(), _proposal())
    assert outcome.accepted
    assert "explanation_refs_grounded" in outcome.checks
    assert "protected_spans_intact" in outcome.checks
    assert "no_prohibited_claims" in outcome.checks
    assert "numbers_grounded" in outcome.checks
    assert "sentence_bounded" in outcome.checks


def test_verify_rejects_ungrounded_ref() -> None:
    outcome = verify_explanation(_context(), _proposal(refs=("episode:made_up",)))
    assert not outcome.accepted
    assert outcome.rejected_reason == "ungrounded_explanation_ref"


def test_verify_defends_empty_refs_at_contract_level() -> None:
    with pytest.raises(ValidationError):
        _proposal(refs=())


def test_verify_rejects_copied_protected_span() -> None:
    outcome = verify_explanation(
        _context(),
        _proposal(
            explanation=(
                "Repeated errors map to the same misconception, so a worked "
                "example isolates the error pattern before more practice."
            )
        ),
    )
    assert not outcome.accepted
    assert outcome.rejected_reason == "protected_span_rewritten"


def test_verify_rejects_lesson_title_copy() -> None:
    outcome = verify_explanation(
        _context(),
        _proposal(explanation="The Sign error worked example is next for you."),
    )
    assert not outcome.accepted
    assert outcome.rejected_reason == "protected_span_rewritten"


def test_verify_rejects_suffix_copy_of_protected_span() -> None:
    outcome = verify_explanation(
        _context(),
        _proposal(
            explanation=(
                "Map to the same misconception, so a worked example "
                "isolates the error pattern before more practice - that is "
                "the plan for this session."
            )
        ),
    )
    assert not outcome.accepted
    assert outcome.rejected_reason == "protected_span_rewritten"


def test_verify_rejects_prohibited_claims() -> None:
    for bad in (
        "This guarantees you will stop making this error.",
        "Your score will improve if you follow this.",
        "You are permanently stuck on this misconception.",
        "This is an expert-approved strategy.",
        "Two mistakes may indicate a diagnosable gap.",
        "Practicing faster works best for you.",
    ):
        outcome = verify_explanation(_context(), _proposal(explanation=bad))
        assert not outcome.accepted, bad
        assert outcome.rejected_reason == "prohibited_claim", bad


def test_verify_rejects_unsafe_formatting() -> None:
    for bad in ("**bold** claim", "See <b>why</b>", "Use `code`", "A | pipe"):
        outcome = verify_explanation(_context(), _proposal(explanation=bad))
        assert not outcome.accepted, bad
        assert outcome.rejected_reason == "unsafe_formatting", bad


def test_verify_rejects_ungrounded_numbers() -> None:
    outcome = verify_explanation(
        _context(),
        _proposal(
            explanation=(
                "Because 3 sign error mistakes were recorded, review the "
                "sign step before practice."
            )
        ),
    )
    assert not outcome.accepted
    assert outcome.rejected_reason == "ungrounded_number"


def test_verify_accepts_grounded_number_from_fact() -> None:
    outcome = verify_explanation(
        _context(),
        _proposal(
            explanation=(
                "Because 2 sign error mistakes were recorded this session, "
                "a worked example shows the pattern and builds mastery."
            )
        ),
    )
    assert outcome.accepted


def test_verify_rejects_multi_sentence_explanation() -> None:
    outcome = verify_explanation(
        _context(),
        _proposal(
            explanation=(
                "Two sign error mistakes were recorded this session. "
                "A worked example shows the pattern. Practice again after."
            )
        ),
    )
    assert not outcome.accepted
    assert outcome.rejected_reason == "too_many_sentences"


# ---------------------------------------------------------------------------
# Gateway: never raises, never changes the action
# ---------------------------------------------------------------------------


def test_run_shadow_explanation_returns_verified_proposal(monkeypatch) -> None:
    monkeypatch.setenv("BRIDGESAT_HYBRID_ENABLED", "1")
    monkeypatch.setenv("BRIDGESAT_HYBRID_EXPLANATION_ENABLED", "1")
    transport = SequenceTransport(
        [
            json.dumps(
                {
                    "student_explanation": (
                        "Because two sign error mistakes were recorded in "
                        "this session, a worked example isolates the pattern "
                        "and builds mastery."
                    ),
                    "emphasis": "process",
                    "evidence_refs": ["stat:misconception"],
                }
            )
        ]
    )
    proposal = run_shadow_explanation(_context(), _client(transport))
    assert proposal is not None
    assert proposal.evidence_refs == ("stat:misconception",)
    assert len(transport.calls) == 1


def test_run_shadow_explanation_returns_none_when_disabled(monkeypatch) -> None:
    monkeypatch.setenv("BRIDGESAT_HYBRID_ENABLED", "1")
    monkeypatch.setenv("BRIDGESAT_HYBRID_EXPLANATION_ENABLED", "0")
    transport = SequenceTransport([json.dumps({})])
    assert run_shadow_explanation(_context(), _client(transport)) is None
    assert transport.calls == []


def test_run_shadow_explanation_returns_none_when_not_configured(monkeypatch) -> None:
    monkeypatch.setenv("BRIDGESAT_HYBRID_ENABLED", "1")
    monkeypatch.setenv("BRIDGESAT_HYBRID_EXPLANATION_ENABLED", "1")
    client = LLMClient(api_key="", model="test/model")
    assert run_shadow_explanation(_context(), client) is None


def test_run_shadow_explanation_returns_none_on_unparsable_output(monkeypatch) -> None:
    monkeypatch.setenv("BRIDGESAT_HYBRID_ENABLED", "1")
    monkeypatch.setenv("BRIDGESAT_HYBRID_EXPLANATION_ENABLED", "1")
    transport = SequenceTransport(["I am sorry, I cannot do that."])
    assert run_shadow_explanation(_context(), _client(transport)) is None


def test_run_shadow_explanation_returns_none_when_verification_fails(monkeypatch) -> None:
    monkeypatch.setenv("BRIDGESAT_HYBRID_ENABLED", "1")
    monkeypatch.setenv("BRIDGESAT_HYBRID_EXPLANATION_ENABLED", "1")
    transport = SequenceTransport(
        [
            json.dumps(
                {
                    "student_explanation": "Your score will rise guaranteed.",
                    "emphasis": "transfer",
                    "evidence_refs": ["stat:mastery"],
                }
            )
        ]
    )
    assert run_shadow_explanation(_context(), _client(transport)) is None


def test_run_shadow_explanation_never_raises_on_failures(monkeypatch) -> None:
    monkeypatch.setenv("BRIDGESAT_HYBRID_ENABLED", "1")
    monkeypatch.setenv("BRIDGESAT_HYBRID_EXPLANATION_ENABLED", "1")
    assert run_shadow_explanation(_context(), _client(FailTransport())) is None
    assert (
        run_shadow_explanation(
            _context(), _client(FailTransport(RuntimeError("boom")))
        )
        is None
    )
