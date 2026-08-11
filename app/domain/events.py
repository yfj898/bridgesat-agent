from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

POLICY_VERSION = "policy-0.1.0"
TAXONOMY_VERSION = "taxonomy-v1.0-draft"


class LearningEventType(StrEnum):
    STUDENT_CREATED = "STUDENT_CREATED"
    DIAGNOSTIC_STARTED = "DIAGNOSTIC_STARTED"
    ANSWER_SUBMITTED = "ANSWER_SUBMITTED"
    ANSWER_EVALUATED = "ANSWER_EVALUATED"
    MISCONCEPTION_IDENTIFIED = "MISCONCEPTION_IDENTIFIED"
    HINT_REQUESTED = "HINT_REQUESTED"
    INTERVENTION_SELECTED = "INTERVENTION_SELECTED"
    CONTENT_PRESENTED = "CONTENT_PRESENTED"
    WORKED_EXAMPLE_PRESENTED = "WORKED_EXAMPLE_PRESENTED"
    PLAN_ADAPTED = "PLAN_ADAPTED"
    SESSION_COMPLETED = "SESSION_COMPLETED"
    EPISODE_CREATED = "EPISODE_CREATED"
    MEMORY_INDEXED = "MEMORY_INDEXED"
    OFFLINE_EVENT_QUEUED = "OFFLINE_EVENT_QUEUED"
    OFFLINE_EVENT_SYNCED = "OFFLINE_EVENT_SYNCED"


Origin = Literal["online", "offline"]


def canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def compute_integrity_hash(event_type: str, payload: dict[str, Any]) -> str:
    digest = hashlib.sha256()
    digest.update(event_type.encode("utf-8"))
    digest.update(b"\x00")
    digest.update(canonical_json(payload).encode("utf-8"))
    return f"sha256:{digest.hexdigest()}"


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


class LearningEvent(BaseModel):
    event_id: str = Field(min_length=1)
    student_id: str
    session_id: str
    event_type: LearningEventType
    payload: dict[str, Any]
    policy_version: str = POLICY_VERSION
    content_version: str | None = None
    occurred_at: str
    received_at: str
    device_id: str | None = None
    device_sequence: int | None = None
    origin: Origin = "online"
    integrity_hash: str | None = None

    def hash_payload(self) -> str:
        return compute_integrity_hash(self.event_type, self.payload)

    def with_integrity(self) -> LearningEvent:
        return self.model_copy(update={"integrity_hash": self.hash_payload()})


class AgentEvent(BaseModel):
    event_id: str = Field(min_length=1)
    student_id: str
    session_id: str
    source_event_id: str | None = None
    state_before: str | None = None
    state_after: str | None = None
    action: str
    action_payload: dict[str, Any] = Field(default_factory=dict)
    reason_code: str
    reason_text: str
    policy_version: str = POLICY_VERSION
    taxonomy_version: str = TAXONOMY_VERSION
    content_version: str | None = None
    referenced_content: list[str] = Field(default_factory=list)
    episode_ids: list[str] = Field(default_factory=list)
    source: Origin = "online"
    created_at: str = Field(default_factory=utc_now_iso)


class AgentDecision(BaseModel):
    action: str
    action_payload: dict[str, Any] = Field(default_factory=dict)
    reason_code: str
    reason_text: str
    target_skill: str | None = None
    difficulty: int | None = None
    content_id: str | None = None
    episode_ids: list[str] = Field(default_factory=list)
    policy_version: str = POLICY_VERSION
    taxonomy_version: str = TAXONOMY_VERSION
    content_version: str | None = None
