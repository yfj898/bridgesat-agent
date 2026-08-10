"""SessionOrchestrator dual-mode decision tests.

With an LLM client injected, the orchestrator asks the LLM for the next
action as structured JSON and falls back to the deterministic policy when the
LLM is unavailable or returns something unparseable. Without a client, the
behavior is byte-identical to the deterministic policy.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import psycopg
import pytest

from app.agent.llm_client import LLMClient
from app.agent.orchestrator import ContentItem, SessionOrchestrator
from app.domain.sessions import SessionState


def _item(content_id: str = "math.linear_equations.001") -> ContentItem:
    return ContentItem(
        content_id=content_id,
        version=1,
        skill="linear_equations",
        subskill="sign_handling",
        difficulty=2,
        answer_choice_id="C",
        misconception_map={"A": "sign_error", "B": "inverse_operation_error", "D": "arithmetic_error"},
    )


def _bring_to_question(o: SessionOrchestrator, student_id: str, session_id: str) -> None:
    o.learner.create_session(student_id, session_id)
    for state in [
        SessionState.PROFILE_READY,
        SessionState.DIAGNOSTIC_ACTIVE,
        SessionState.DIAGNOSTIC_COMPLETE,
        SessionState.PLAN_READY,
        SessionState.QUESTION_ACTIVE,
    ]:
        o.learner.transition_session(session_id, state)


class StubLLM:
    """Configurable LLM stub: returns a fixed answer or fails like a real
    client (LLMUnavailableError) after recording the prompt."""

    def __init__(self, content: str | None, *, fail: bool = False) -> None:
        self.content = content
        self.fail = fail
        self.prompts: list[str] = []

    async def complete(self, prompt: str, **kwargs: Any) -> str:
        self.prompts.append(prompt)
        if self.fail:
            from app.agent.llm_client import LLMUnavailableError

            raise LLMUnavailableError("stub down")
        return self.content or ""


@pytest.fixture()
def orchestrator(pg_connection: psycopg.Connection) -> SessionOrchestrator:
    return SessionOrchestrator(pg_connection)


def _decision(o: SessionOrchestrator, student_id: str, session_id: str, item: ContentItem | None = None) -> dict:
    return o.evaluate_answer(
        student_id=student_id,
        session_id=session_id,
        item=item or _item(),
        selected_choice_id="A",
        hint_level=0,
        minutes_remaining=20,
    ).decision


def _two_sign_errors(o: SessionOrchestrator, student_id: str, session_id: str) -> dict:
    """Two distinct sign-error answers: the deterministic policy escalates to
    SHOW_WORKED_EXAMPLE on the second one."""
    first = _decision(o, student_id, session_id, _item("math.linear_equations.001"))
    assert first.action == "RETRY_SAME_SKILL"
    return _decision(o, student_id, session_id, _item("math.linear_equations.002"))


def test_without_llm_uses_deterministic_policy(orchestrator: SessionOrchestrator) -> None:
    student_id, _ = orchestrator.learner.create_student("Ari", 20, 1200)
    session_id = f"ses-{student_id}"
    _bring_to_question(orchestrator, student_id, session_id)

    decision = _two_sign_errors(orchestrator, student_id, session_id)

    assert decision.action == "SHOW_WORKED_EXAMPLE"
    assert decision.reason_code == "REPEATED_MISCONCEPTION"


def test_llm_decision_overrides_policy(orchestrator: SessionOrchestrator) -> None:
    llm = StubLLM(
        json.dumps(
            {
                "action": "GIVE_HINT_1",
                "reason_code": "LLM_REASONED",
                "reason_text": "student misread the sign",
            }
        )
    )
    orchestrator.llm = llm
    student_id, _ = orchestrator.learner.create_student("Bo", 20, 1200)
    session_id = f"ses-{student_id}"
    _bring_to_question(orchestrator, student_id, session_id)

    decision = _decision(orchestrator, student_id, session_id)

    assert decision.action == "GIVE_HINT_1"
    assert decision.reason_code == "LLM_REASONED"
    assert decision.policy_version == "llm-0.1.0"
    assert len(llm.prompts) == 1
    assert "linear_equations" in llm.prompts[0]


def test_llm_failure_falls_back_to_policy(orchestrator: SessionOrchestrator) -> None:
    orchestrator.llm = StubLLM(None, fail=True)
    student_id, _ = orchestrator.learner.create_student("Cy", 20, 1200)
    session_id = f"ses-{student_id}"
    _bring_to_question(orchestrator, student_id, session_id)

    decision = _two_sign_errors(orchestrator, student_id, session_id)

    assert decision.action == "SHOW_WORKED_EXAMPLE"
    assert decision.reason_code == "REPEATED_MISCONCEPTION"


def test_llm_garbage_output_falls_back_to_policy(orchestrator: SessionOrchestrator) -> None:
    orchestrator.llm = StubLLM("not json at all")
    student_id, _ = orchestrator.learner.create_student("De", 20, 1200)
    session_id = f"ses-{student_id}"
    _bring_to_question(orchestrator, student_id, session_id)

    decision = _two_sign_errors(orchestrator, student_id, session_id)

    assert decision.action == "SHOW_WORKED_EXAMPLE"


def test_llm_unknown_action_falls_back_to_policy(orchestrator: SessionOrchestrator) -> None:
    orchestrator.llm = StubLLM(json.dumps({"action": "DELETE_EVERYTHING", "reason_code": "X", "reason_text": "y"}))
    student_id, _ = orchestrator.learner.create_student("Ef", 20, 1200)
    session_id = f"ses-{student_id}"
    _bring_to_question(orchestrator, student_id, session_id)

    decision = _two_sign_errors(orchestrator, student_id, session_id)

    assert decision.action == "SHOW_WORKED_EXAMPLE"
