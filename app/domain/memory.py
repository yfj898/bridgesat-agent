from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

EPISODE_STATUSES = (
    "candidate",
    "validated",
    "insufficient_outcome",
    "contradicted",
    "archived",
    "deleted",
)
EPISODE_MIN_CONFIDENCE = 0.50

FACT_STATUSES = ("observation", "inference", "stable", "uncertain", "archived")
FACT_PROMOTION_MIN_CONFIDENCE = 0.70

INTERVENTION_WINDOWS = ("immediate", "short_term", "delayed")


class BoundedAction(StrEnum):
    ASK_QUESTION = "ASK_QUESTION"
    GIVE_HINT_1 = "GIVE_HINT_1"
    GIVE_HINT_2 = "GIVE_HINT_2"
    GIVE_HINT_3 = "GIVE_HINT_3"
    SHOW_MICRO_LESSON = "SHOW_MICRO_LESSON"
    SHOW_WORKED_EXAMPLE = "SHOW_WORKED_EXAMPLE"
    RETRY_SAME_SKILL = "RETRY_SAME_SKILL"
    LOWER_DIFFICULTY = "LOWER_DIFFICULTY"
    RAISE_DIFFICULTY = "RAISE_DIFFICULTY"
    SWITCH_TO_PREREQUISITE = "SWITCH_TO_PREREQUISITE"
    SCHEDULE_REVIEW = "SCHEDULE_REVIEW"
    END_WITH_REVIEW = "END_WITH_REVIEW"
    END_SESSION = "END_SESSION"


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


class Episode(BaseModel):
    episode_id: str
    student_id: str
    session_id: str
    skill: str
    misconception: str | None = None
    intervention: str
    outcome: dict[str, Any]
    effectiveness: float = Field(ge=0.0, le=1.0)
    evidence_event_ids: list[str]
    summary: str
    confidence: float = Field(ge=0.0, le=1.0)
    status: Literal[
        "candidate",
        "validated",
        "insufficient_outcome",
        "contradicted",
        "archived",
        "deleted",
    ] = "candidate"
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)

    def is_validated(self) -> bool:
        return self.status == "validated"


class MemoryFact(BaseModel):
    fact_id: str
    student_id: str
    category: str
    normalized_key: str
    fact_text: str
    confidence: float = Field(ge=0.0, le=1.0)
    supporting_episode_ids: list[str] = Field(default_factory=list)
    contradicting_episode_ids: list[str] = Field(default_factory=list)
    evidence_count: int = 0
    contradiction_count: int = 0
    status: Literal["observation", "inference", "stable", "uncertain", "archived"] = "observation"
    first_observed_at: str = Field(default_factory=utc_now_iso)
    last_observed_at: str = Field(default_factory=utc_now_iso)
    version: int = 1


class InterventionStat(BaseModel):
    stat_id: str
    student_id: str
    skill: str
    misconception: str | None = None
    intervention: str
    difficulty_band: str
    immediate_correct: float = 0.0
    immediate_attempts: int = 0
    immediate_weight: float = 0.0
    short_term_correct: float = 0.0
    short_term_attempts: int = 0
    short_term_weight: float = 0.0
    delayed_correct: float = 0.0
    delayed_attempts: int = 0
    delayed_weight: float = 0.0
    updated_at: str = Field(default_factory=utc_now_iso)

    def effectiveness(self, window: str) -> float | None:
        attempts = getattr(self, f"{window}_attempts")
        if attempts == 0:
            return None
        weight = getattr(self, f"{window}_weight")
        if weight <= 0:
            return None
        return getattr(self, f"{window}_correct") / weight

    def blended_effectiveness(self) -> float | None:
        weights = {"immediate": 0.50, "short_term": 0.30, "delayed": 0.20}
        total_weight = 0.0
        total_score = 0.0
        for window, window_weight in weights.items():
            attempts = getattr(self, f"{window}_attempts")
            if attempts == 0:
                continue
            weight = getattr(self, f"{window}_weight")
            if weight <= 0:
                continue
            total_weight += window_weight
            total_score += (
                window_weight * getattr(self, f"{window}_correct") / weight
            )
        if total_weight <= 0:
            return None
        return total_score / total_weight


def outcome_component_score(correct: bool, hint_level: int) -> float:
    if not correct:
        return 0.0
    if hint_level == 0:
        return 1.0
    if hint_level == 1:
        return 0.8
    if hint_level == 2:
        return 0.5
    return 0.2
