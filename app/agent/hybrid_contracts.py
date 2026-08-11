"""Strict Hybrid reasoning contracts (Hybrid Integration Plan Section 10).

All models are fail-closed: ``extra="forbid"``, bounded strings/lists, strict
enums, no raw database objects, no student identifiers, no PII, no prompts,
and no provider secrets. Only a sanitized verified result may reach an
``AgentEvent``; raw model output never leaves the gateway.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.domain.events import AgentDecision
from app.domain.memory import BoundedAction
from app.domain.sessions import SessionState

CONTEXT_VERSION = "hybrid-context-0.1.0"

RECENCY_BUCKETS = ("recent", "medium", "older")
CLAIM_CODES = (
    "SAME_MISCONCEPTION",
    "SUCCESSFUL_TRANSFER",
    "SUPPORTED_INTERVENTION_EFFECT",
    "STUDENT_REASONING_SIGNAL",
)


class RecalledEpisodeEvidence(BaseModel):
    """Scoped, rehydrated PostgreSQL episode exposed to a model task."""

    model_config = {"extra": "forbid"}

    episode_id: str = Field(min_length=1, max_length=64)
    skill: str = Field(min_length=1, max_length=64)
    misconception: str | None = Field(default=None, max_length=64)
    intervention: BoundedAction
    outcome_correct: bool
    different_item: bool = False
    effectiveness: float = Field(ge=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    status: Literal["validated"]
    recency_bucket: Literal["recent", "medium", "older"]
    teaching_content_id: str | None = Field(default=None, max_length=64)
    difficulty_band: str | None = Field(default=None, max_length=8)


class InterventionEvidence(BaseModel):
    """Supported InterventionStat aggregate, labeled unavailable when the
    sample gate is not met (never presented as false precision)."""

    model_config = {"extra": "forbid"}

    intervention: BoundedAction
    difficulty_band: str = Field(min_length=1, max_length=8)
    immediate_attempts: int = Field(ge=0, le=1000)
    short_term_attempts: int = Field(ge=0, le=1000)
    delayed_attempts: int = Field(ge=0, le=1000)
    blended_effectiveness: float | None = Field(default=None, ge=0.0, le=1.0)
    support: Literal["insufficient", "supported"]


class ContentCandidate(BaseModel):
    """Approved, version-bound content candidate."""

    model_config = {"extra": "forbid"}

    content_id: str = Field(min_length=1, max_length=64)
    content_type: Literal["worked_example", "micro_lesson"]
    skill: str = Field(min_length=1, max_length=64)
    misconceptions: tuple[str, ...] = ()
    pack_version: str = Field(min_length=1, max_length=32)
    content_hash: str = Field(min_length=8, max_length=128)
    review_status: Literal["approved"]
    human_approved: bool


class HybridDecisionContext(BaseModel):
    """Minimal, scoped, structured input for the intervention-ranking task.

    Deliberately excluded: student identifier, name/email, raw full history,
    unrelated skills, prompt secrets, database/tenant configuration, and
    unbounded free text.
    """

    model_config = {"extra": "forbid"}

    task: Literal["intervention_ranking"]
    context_version: str = CONTEXT_VERSION
    skill: str = Field(min_length=1, max_length=64)
    subskill: str | None = Field(default=None, max_length=64)
    difficulty: int = Field(ge=1, le=3)
    mastery: float = Field(ge=0.0, le=1.0)
    mastery_confidence: float = Field(ge=0.0, le=1.0)
    consecutive_errors: int = Field(ge=0, le=64)
    correct_streak: int = Field(ge=0, le=64)
    active_misconception: str | None = Field(default=None, max_length=64)
    misconception_evidence_count: int = Field(ge=0, le=256)
    misconception_confidence: Literal["low", "medium", "high"]
    hints_used: int = Field(ge=0, le=3)
    minutes_remaining: int = Field(ge=0, le=300)
    current_state: SessionState
    allowed_actions: tuple[BoundedAction, ...] = Field(min_length=1, max_length=8)
    deterministic_fallback: AgentDecision
    recalled_episodes: tuple[RecalledEpisodeEvidence, ...] = Field(max_length=16)
    intervention_stats: tuple[InterventionEvidence, ...] = Field(max_length=8)
    content_candidates: tuple[ContentCandidate, ...] = Field(max_length=8)


class EvidenceClaim(BaseModel):
    """Structured, verifiable rationale claim with evidence references."""

    model_config = {"extra": "forbid"}

    claim_code: Literal[
        "SAME_MISCONCEPTION",
        "SUCCESSFUL_TRANSFER",
        "SUPPORTED_INTERVENTION_EFFECT",
        "STUDENT_REASONING_SIGNAL",
    ]
    evidence_refs: tuple[str, ...] = Field(max_length=8)


class HybridDecisionProposal(BaseModel):
    """Structured model proposal; never trusted until the verifier accepts it."""

    model_config = {"extra": "forbid"}

    proposed_action: BoundedAction
    selected_episode_id: str | None = Field(default=None, max_length=64)
    selected_content_id: str | None = Field(default=None, max_length=64)
    rationale_code: str = Field(min_length=1, max_length=64)
    rationale: str = Field(max_length=320)
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_claims: tuple[EvidenceClaim, ...] = Field(max_length=8)


class VerifiedHybridDecision(BaseModel):
    """The only Hybrid output permitted to reach an AgentEvent or the PWA."""

    model_config = {"extra": "forbid"}

    accepted: bool
    final_action: BoundedAction
    model_used: bool
    fallback_used: bool
    fallback_reason: str | None = Field(default=None, max_length=128)
    verification_checks: tuple[str, ...] = ()
    selected_episode_id: str | None = Field(default=None, max_length=64)
    selected_content_id: str | None = Field(default=None, max_length=64)
    safe_student_explanation: str | None = Field(default=None, max_length=320)
    model_task: str | None = Field(default=None, max_length=64)
    model_name: str | None = Field(default=None, max_length=128)
    prompt_version: str | None = Field(default=None, max_length=64)
    latency_ms: int | None = Field(default=None, ge=0, le=600_000)


class HybridShadowObservation(BaseModel):
    """Sanitized post-commit shadow observation (Hybrid Plan H4).

    Produced only after the authoritative answer/AgentEvent has committed and
    the student advisory lock/transaction have released. It never changes the
    executed teaching action; ``would_change`` only records that the verified
    model proposal differed from the deterministic fallback. No PII, no raw
    model text, no provider details.
    """

    model_config = {"extra": "forbid"}

    source_event_id: str = Field(min_length=1, max_length=64)
    fallback_action: BoundedAction
    model_proposal_action: BoundedAction | None = None
    accepted: bool
    would_change: bool
    rejection_reason: str | None = Field(default=None, max_length=128)
    latency_ms: int = Field(ge=0, le=600_000)
    verification_checks: tuple[str, ...] = ()


class ExplanationProposal(BaseModel):
    """Grounded personalized wording (H5); never reopens action selection."""

    model_config = {"extra": "forbid"}

    student_explanation: str = Field(max_length=320)
    emphasis: Literal["process", "sign", "setup", "transfer", "review"]
    evidence_refs: tuple[str, ...] = Field(max_length=8)