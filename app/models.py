from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class Skill(StrEnum):
    LINEAR_EQUATIONS = "linear_equations"
    RATIOS = "ratios"
    READING_INFERENCE = "reading_inference"
    SYSTEMS_EQUATIONS = "systems_equations"
    RATIOS_PERCENTAGES = "ratios_percentages"
    FUNCTIONS_MODELS = "functions_models"
    INEQUALITIES = "inequalities"
    QUADRATIC_EQUATIONS = "quadratic_equations"
    EXPONENTS_RADICALS = "exponents_radicals"
    COORDINATE_GEOMETRY = "coordinate_geometry"


class StudentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    daily_minutes: int = Field(default=20, ge=5, le=120)
    target_score: int = Field(default=1200, ge=400, le=1600)


class Student(StudentCreate):
    id: str
    mastery: dict[Skill, float]


class StudentWithToken(Student):
    """Student creation response carrying the one-time bearer token.

    The token is returned exactly once; only its SHA-256 hash is stored
    (THREAT_MODEL section 10, plan section 7).
    """

    token: str


class DiagnosticAnswer(BaseModel):
    question_id: str
    selected_answer: str
    hint_level: int = Field(default=0, ge=0, le=3)


class DiagnosticRequest(BaseModel):
    answers: list[DiagnosticAnswer] = Field(min_length=1)


class PlanItem(BaseModel):
    activity: Literal["micro_lesson", "practice", "review", "reflection"]
    skill: Skill
    minutes: int = Field(ge=1)
    reason: str


class DiagnosticResponse(BaseModel):
    student_id: str
    mastery: dict[Skill, float]
    weakest_skills: list[Skill]
    plan: list[PlanItem]
    agent_explanation: str


class AdaptRequest(BaseModel):
    skill: Skill
    was_correct: bool
    hint_level: int = Field(default=0, ge=0, le=3)
    consecutive_skill_errors: int = Field(default=0, ge=0, le=20)
    minutes_remaining: int = Field(default=10, ge=0, le=120)


class AdaptResponse(BaseModel):
    action: Literal[
        "increase_difficulty",
        "continue_practice",
        "decrease_difficulty",
        "insert_micro_lesson",
        "end_with_review",
    ]
    mastery: float = Field(ge=0.0, le=1.0)
    reason: str
    next_difficulty_delta: int = Field(ge=-1, le=1)


class Question(BaseModel):
    id: str
    skill: Skill
    difficulty: int = Field(ge=1, le=5)
    prompt: str
    choices: list[str] = Field(min_length=2)
    answer: str
    hints: list[str] = Field(min_length=1, max_length=3)
    explanation: str
