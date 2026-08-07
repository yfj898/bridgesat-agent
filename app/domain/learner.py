from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

DEFAULT_ALPHA = 2.0
DEFAULT_BETA = 2.0

DIFFICULTY_WEIGHT = {1: 0.75, 2: 1.00, 3: 1.25}
HINT_MULTIPLIER = {0: 1.00, 1: 0.80, 2: 0.55, 3: 0.30}
REPEAT_SAME_ITEM_MULTIPLIER = 0.35
IMMEDIATE_TRANSFER_MULTIPLIER = 1.10
INVALID_EVIDENCE_MULTIPLIER = 0.00

MASTERY_PROMOTION_THRESHOLD = 0.72
CONFIDENCE_PROMOTION_THRESHOLD = 0.55
MASTERY_SUPPORT_THRESHOLD = 0.45
CONFIDENCE_SUPPORT_THRESHOLD = 0.40

STALENESS_DAYS = 14
STALENESS_DECAY_PER_WEEK = 0.98

MISCONCEPTION_STATE_ORDER = ["observed", "suspected", "confirmed", "resolved", "archived"]


class MisconceptionState(StrEnum):
    OBSERVED = "observed"
    SUSPECTED = "suspected"
    CONFIRMED = "confirmed"
    RESOLVED = "resolved"
    ARCHIVED = "archived"


class EvidenceWeight(BaseModel):
    difficulty: int = Field(ge=1, le=3)
    hint_level: int = Field(ge=0, le=3)
    repeated_same_item: bool = False
    immediate_transfer: bool = False
    validity_multiplier: float = Field(default=1.0, ge=0.0, le=1.0)

    def weight(self) -> float:
        return (
            DIFFICULTY_WEIGHT.get(self.difficulty, 1.0)
            * HINT_MULTIPLIER.get(self.hint_level, 1.0)
            * (REPEAT_SAME_ITEM_MULTIPLIER if self.repeated_same_item else 1.0)
            * (IMMEDIATE_TRANSFER_MULTIPLIER if self.immediate_transfer else 1.0)
            * self.validity_multiplier
        )


class SkillState(BaseModel):
    skill: str
    alpha: float = DEFAULT_ALPHA
    beta: float = DEFAULT_BETA
    evidence_count: int = 0
    correct_streak: int = 0
    incorrect_streak: int = 0
    last_practiced_at: str | None = None
    review_due_at: str | None = None
    projection_origin: str = "live"

    @property
    def mastery(self) -> float:
        return self.alpha / (self.alpha + self.beta)

    @property
    def confidence(self) -> float:
        return min(1.0, max(0.0, (self.alpha + self.beta - 4.0) / 8.0))

    def record_attempt(self, correct: bool, weight: float, now: str) -> None:
        if correct:
            self.alpha += weight
            self.correct_streak += 1
            self.incorrect_streak = 0
        else:
            self.beta += weight
            self.incorrect_streak += 1
            self.correct_streak = 0
        self.evidence_count += 1
        self.last_practiced_at = now

    def decay_confidence(self, now: str) -> None:
        if self.last_practiced_at is None:
            return
        last = datetime.fromisoformat(self.last_practiced_at)
        current = datetime.fromisoformat(now)
        inactive_days = (current - last).total_seconds() / 86400
        if inactive_days <= STALENESS_DAYS:
            return
        extra_weeks = (inactive_days - STALENESS_DAYS) / 7.0
        decay = STALENESS_DECAY_PER_WEEK ** extra_weeks
        new_confidence = self.confidence * decay
        self.review_due_at = (
            last + timedelta(days=STALENESS_DAYS)
        ).isoformat()


def should_promote_difficulty(
    *,
    mastery: float,
    confidence: float,
    recent_correct_without_high_hint: int,
    recent_total: int,
    has_active_high_confidence_misconception: bool,
) -> bool:
    if mastery < MASTERY_PROMOTION_THRESHOLD:
        return False
    if confidence < CONFIDENCE_PROMOTION_THRESHOLD:
        return False
    if has_active_high_confidence_misconception:
        return False
    if recent_total < 3:
        return False
    return recent_correct_without_high_hint >= 2


def should_support(
    *,
    mastery: float,
    confidence: float,
    consecutive_errors: int,
    repeated_misconception: bool,
    requires_unmastered_prerequisite: bool,
) -> bool:
    if consecutive_errors >= 2:
        return True
    if repeated_misconception:
        return True
    if mastery < MASTERY_SUPPORT_THRESHOLD and confidence >= CONFIDENCE_SUPPORT_THRESHOLD:
        return True
    return requires_unmastered_prerequisite


class MisconceptionEvidence(BaseModel):
    evidence_id: str
    student_id: str
    session_id: str
    event_id: str
    skill: str
    subskill: str | None = None
    misconception: str
    source_label: Literal["distractor_mapping", "symbolic_rule", "behavioral_pattern", "llm_classification"]
    confidence_label: Literal["high", "medium", "low"]
    state: MisconceptionState = MisconceptionState.OBSERVED
    item_id: str
    item_version: int
    observed_at: str


def next_misconception_state(observation_count: int, distinct_items: int) -> MisconceptionState:
    if observation_count >= 3 and distinct_items >= 2:
        return MisconceptionState.CONFIRMED
    if observation_count >= 2:
        return MisconceptionState.SUSPECTED
    return MisconceptionState.OBSERVED
