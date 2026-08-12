from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

import psycopg

from app.agent.policy import PolicyInput, decide_next_action
from app.domain.events import (
    AgentDecision,
    AgentEvent,
    LearningEvent,
    LearningEventType,
    utc_now_iso,
)
from app.domain.learner import EvidenceWeight
from app.domain.memory import BoundedAction, Episode, outcome_component_score
from app.domain.sessions import SessionState
from app.infrastructure.event_store import EventStore
from app.infrastructure.learner_store import LearnerStore
from app.memory.episode_builder import EpisodeBuilder
from app.memory.pg_memory import PGMemory

MISCONCEPTION_MAP_FIELD = "misconception_map"


@dataclass
class ContentItem:
    content_id: str
    version: int
    skill: str
    subskill: str
    difficulty: int
    answer_choice_id: str
    misconception_map: dict[str, str] = field(default_factory=dict)


@dataclass
class DecisionOutcome:
    decision: AgentDecision
    next_state: SessionState
    agent_event: AgentEvent
    episode: Episode | None = None


def _await_in_any_context(coro):
    """Compatibility alias; moved to app.infrastructure.async_utils."""
    from app.infrastructure.async_utils import await_in_any_context as _impl

    return _impl(coro)


def _policy_input_brief(inputs: PolicyInput) -> dict:
    """A compact, serializable view of the state for the LLM prompt."""
    return {
        "skill": inputs.skill,
        "subskill": inputs.subskill,
        "difficulty": inputs.difficulty,
        "mastery": round(inputs.mastery, 3),
        "confidence": round(inputs.confidence, 3),
        "consecutive_errors": inputs.consecutive_errors,
        "correct_streak": inputs.correct_streak,
        "repeated_misconception": inputs.repeated_misconception,
        "active_misconception": inputs.active_misconception,
        "misconception_observation_count": inputs.misconception_observation_count,
        "requires_unmastered_prerequisite": inputs.requires_unmastered_prerequisite,
        "minutes_remaining": inputs.minutes_remaining,
        "hints_used_this_item": inputs.hints_used_this_item,
        "recalled_successful_interventions": inputs.recalled_successful_interventions,
    }


def _next_state_for(action: str) -> SessionState:
    """Map a bounded action to the session state it leads to."""
    from app.domain.memory import BoundedAction as BA

    if action == BA.SHOW_WORKED_EXAMPLE.value:
        return SessionState.WORKED_EXAMPLE_ACTIVE
    if action == BA.SHOW_MICRO_LESSON.value:
        return SessionState.MICRO_LESSON_ACTIVE
    if action in (BA.GIVE_HINT_1.value, BA.GIVE_HINT_2.value, BA.GIVE_HINT_3.value):
        return SessionState.QUESTION_ACTIVE
    if action in (BA.SCHEDULE_REVIEW.value, BA.END_WITH_REVIEW.value, BA.END_SESSION.value):
        return SessionState.SESSION_SUMMARY
    return SessionState.QUESTION_ACTIVE


class SessionOrchestrator:
    """Ties immutable events, projections, memory, and the bounded policy into
    one explainable decision per interaction.

    Dual-mode decision: when ``llm`` (an LLMClient) is attached, the
    orchestrator asks it for the next action as structured JSON. Any failure,
    unparseable output, or action outside the bounded set falls back to the
    deterministic policy, so an LLM outage never breaks a session. Without an
    attached client the behavior is identical to the deterministic policy.
    """

    def __init__(self, connection: psycopg.Connection, llm=None) -> None:
        self.connection = connection
        self.events = EventStore(connection)
        self.learner = LearnerStore(connection)
        self.memory = PGMemory(connection)
        self.episodes = EpisodeBuilder(connection)
        self.llm = llm

    def _content_version(self, item: ContentItem) -> str:
        return f"{item.content_id}.v{item.version}"

    def evaluate_answer(
        self,
        *,
        student_id: str,
        session_id: str,
        item: ContentItem,
        selected_choice_id: str,
        hint_level: int,
        minutes_remaining: int,
        device_id: str | None = None,
        origin: str = "online",
    ) -> DecisionOutcome:
        correct = selected_choice_id == item.answer_choice_id
        weight = EvidenceWeight(
            difficulty=item.difficulty,
            hint_level=hint_level,
        ).weight()
        now = utc_now_iso()

        eval_event = LearningEvent(
            event_id=f"evt_{uuid.uuid4().hex[:16]}",
            student_id=student_id,
            session_id=session_id,
            event_type=LearningEventType.ANSWER_EVALUATED,
            payload={
                "content_id": item.content_id,
                "version": item.version,
                "selected_choice_id": selected_choice_id,
                "correct": correct,
                "hint_level": hint_level,
            },
            content_version=self._content_version(item),
            occurred_at=now,
            received_at=now,
            device_id=device_id,
            origin=origin,
        ).with_integrity()

        misconception = None
        if not correct:
            misconception = item.misconception_map.get(selected_choice_id)

        evidence, skill_state = self.learner.record_answer_evaluation(
            student_id=student_id,
            session_id=session_id,
            event=eval_event,
            content_id=item.content_id,
            content_version=item.version,
            skill=item.skill,
            subskill=item.subskill,
            difficulty=item.difficulty,
            sequence=0,
            selected_choice_id=selected_choice_id,
            correct=correct,
            hint_level=hint_level,
            weight=weight,
            validity="valid",
            misconception=misconception,
            misconception_source_label="distractor_mapping",
            misconception_confidence_label="high",
            session_state=SessionState.ANSWER_EVALUATED,
        )

        obs_count, distinct_items = (
            self.learner.count_misconception_evidence(student_id, item.skill, misconception)
            if misconception
            else (0, 0)
        )

        recalled = self._recall_episodes(student_id, item.skill, misconception)
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
            student_id=student_id,
            session_id=session_id,
            skill=item.skill,
            subskill=item.subskill,
            difficulty=item.difficulty,
            mastery=skill_state.mastery if skill_state else 0.5,
            confidence=skill_state.confidence if skill_state else 0.0,
            consecutive_errors=skill_state.incorrect_streak if skill_state else 0,
            correct_streak=skill_state.correct_streak if skill_state else 0,
            active_misconception=misconception,
            misconception_observation_count=obs_count,
            misconception_distinct_items=distinct_items,
            minutes_remaining=minutes_remaining,
            recalled_episode_ids=[e.episode_id for e in recalled],
            recalled_successful_interventions=list(recalled_ids_by_intervention),
            recalled_episode_ids_by_intervention=recalled_ids_by_intervention,
        )
        result = self._decide(inputs)

        agent_event = AgentEvent(
            event_id=f"agt_{uuid.uuid4().hex[:16]}",
            student_id=student_id,
            session_id=session_id,
            source_event_id=eval_event.event_id,
            state_before=SessionState.QUESTION_ACTIVE.value,
            state_after=result.next_state.value,
            action=result.decision.action,
            action_payload=result.decision.action_payload,
            reason_code=result.decision.reason_code,
            reason_text=result.decision.reason_text,
            policy_version=result.decision.policy_version,
            content_version=self._content_version(item),
            referenced_content=[item.content_id],
            episode_ids=result.decision.episode_ids,
            source=origin,
            created_at=now,
        )
        self.events.append_agent_event(agent_event)

        self.learner.transition_session(session_id, result.next_state)

        return DecisionOutcome(
            decision=result.decision,
            next_state=result.next_state,
            agent_event=agent_event,
        )

    def _recall_episodes(self, student_id: str, skill: str, misconception: str | None):
        kwargs = {"student_id": student_id, "skill": skill, "limit": 3}
        if misconception is not None:
            kwargs["misconception"] = misconception
        return self.memory.recall_episodes(**kwargs)

    def _decide(self, inputs: PolicyInput):
        """Dual-mode next-action selection.

        With an LLM attached, request a structured decision; fall back to the
        bounded policy when the LLM is unavailable, returns non-JSON, or picks
        an action outside the bounded set. The deterministic policy is always
        the floor, so an LLM outage degrades the decision, never the session.
        """
        if self.llm is not None:
            llm_result = self._decide_with_llm(inputs)
            if llm_result is not None:
                return llm_result
        return decide_next_action(inputs)

    def _decide_with_llm(self, inputs: PolicyInput):
        from app.agent.llm_client import LLMUnavailableError
        import json as _json

        prompt = (
            "You are the next-action policy for an SAT math tutor. Given the "
            "current state, choose exactly one action from the bounded set: "
            + ", ".join(action.value for action in BoundedAction)
            + ". Respond with JSON only: "
            '{"action": "<ACTION>", "reason_code": "<UPPER_SNAKE>", '
            '"reason_text": "<short explanation>"}. '
            "State: " + _json.dumps(_policy_input_brief(inputs), sort_keys=True)
        )
        try:
            content = self.llm.complete(prompt, max_tokens=120, temperature=0.0)
            if hasattr(content, "__await__"):
                from app.infrastructure.async_utils import await_in_any_context

                content = await_in_any_context(content)
        except (LLMUnavailableError, AttributeError, TypeError):
            return None
        if not content:
            return None
        try:
            parsed = _json.loads(content.strip())
        except ValueError:
            return None
        action = parsed.get("action")
        if action not in {a.value for a in BoundedAction}:
            return None
        decision = AgentDecision(
            action=action,
            action_payload={"skill": inputs.skill, "source": "llm"},
            reason_code=str(parsed.get("reason_code") or "LLM_DECISION"),
            reason_text=str(parsed.get("reason_text") or "LLM-selected next action."),
            target_skill=inputs.skill,
            difficulty=inputs.difficulty,
            policy_version="llm-0.1.0",
        )
        from app.agent.policy import PolicyResult

        return PolicyResult(
            decision=decision,
            next_state=_next_state_for(action),
        )

    def build_episode(
        self,
        *,
        student_id: str,
        session_id: str,
        skill: str,
        misconception: str | None,
        intervention: str,
        teaching_content_id: str,
        outcome_item: ContentItem,
        outcome_correct: bool,
        outcome_hint_level: int,
        summary: str,
        evidence_event_ids: list[str] | None = None,
        context_event_id: str | None = None,
        outcome_event_id: str | None = None,
    ) -> Episode | None:
        """Build and validate an episode from a teaching event and a distinct
        transfer outcome. Evidence event IDs default to the session's
        ANSWER_EVALUATED events when not supplied."""
        if evidence_event_ids is None or context_event_id is None or outcome_event_id is None:
            events = self.events.get_learning_events(session_id=session_id)
            evidence_event_ids = [e.event_id for e in events]
            context_event_id = events[0].event_id if events else None
            outcome_event_id = events[-1].event_id if events else None
        episode = self.episodes.build_candidate(
            student_id=student_id,
            session_id=session_id,
            skill=skill,
            misconception=misconception,
            intervention=intervention,
            context_event=self._dummy_context_event(student_id, session_id, context_event_id),
            evidence_events=[
                self._dummy_outcome_event(student_id, session_id, eid)
                for eid in evidence_event_ids
            ],
            outcome_event=self._dummy_outcome_event(student_id, session_id, outcome_event_id),
            outcome_correct=outcome_correct,
            outcome_hint_level=outcome_hint_level,
            outcome_content_id=outcome_item.content_id,
            teaching_content_id=teaching_content_id,
            summary=summary,
        )
        return self.episodes.validate(episode)

    def _dummy_context_event(
        self, student_id: str, session_id: str, event_id: str | None = None
    ) -> LearningEvent:
        return LearningEvent(
            event_id=event_id or f"ctx_{uuid.uuid4().hex[:16]}",
            student_id=student_id,
            session_id=session_id,
            event_type=LearningEventType.CONTENT_PRESENTED,
            payload={},
            occurred_at=utc_now_iso(),
            received_at=utc_now_iso(),
        )

    def _dummy_outcome_event(
        self, student_id: str, session_id: str, event_id: str | None = None
    ) -> LearningEvent:
        return LearningEvent(
            event_id=event_id or f"out_{uuid.uuid4().hex[:16]}",
            student_id=student_id,
            session_id=session_id,
            event_type=LearningEventType.ANSWER_EVALUATED,
            payload={},
            occurred_at=utc_now_iso(),
            received_at=utc_now_iso(),
        )
