"""Sync protocol contracts (SYNC_PROTOCOL.md) and offline policy version."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

OFFLINE_POLICY_VERSION = "offline-policy-v1"
MAX_EVENTS_PER_BATCH = 100


class SyncErrorCode(StrEnum):
    INVALID_SCHEMA = "INVALID_SCHEMA"
    UNAUTHORIZED_STUDENT = "UNAUTHORIZED_STUDENT"
    DEVICE_REVOKED = "DEVICE_REVOKED"
    DUPLICATE_EVENT = "DUPLICATE_EVENT"
    ATTEMPT_ALREADY_SCORED = "ATTEMPT_ALREADY_SCORED"
    QUESTION_VERSION_UNKNOWN = "QUESTION_VERSION_UNKNOWN"
    CONTENT_WITHDRAWN = "CONTENT_WITHDRAWN"
    MISSING_DEPENDENCY = "MISSING_DEPENDENCY"
    SESSION_EXPIRED = "SESSION_EXPIRED"
    SESSION_BRANCH_CONFLICT = "SESSION_BRANCH_CONFLICT"
    PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"
    RATE_LIMITED = "RATE_LIMITED"
    INTERNAL_RETRYABLE = "INTERNAL_RETRYABLE"


class ConflictType(StrEnum):
    PARALLEL_ATTEMPT_DETECTED = "PARALLEL_ATTEMPT_DETECTED"
    SESSION_BRANCH_CONFLICT = "SESSION_BRANCH_CONFLICT"
    SUMMARY_REVISED = "SUMMARY_REVISED"
    ATTEMPT_ALREADY_SCORED = "ATTEMPT_ALREADY_SCORED"


class SyncEventEnvelope(BaseModel):
    event_id: str = Field(min_length=1, max_length=64)
    student_id: str
    session_id: str
    session_branch_id: str
    device_id: str
    device_sequence: int = Field(ge=1)
    event_type: str
    payload: dict
    content_pack_version: str
    question_id: str | None = None
    question_version: int | None = None
    policy_version: str = OFFLINE_POLICY_VERSION
    depends_on_event_ids: list[str] = []
    device_occurred_at: str
    integrity_hash: str | None = None


class SyncRejectedEvent(BaseModel):
    event_id: str
    code: str
    retryable: bool


class SyncConflict(BaseModel):
    event_id: str
    conflict_type: str
    detail: str = ""


class SyncRequest(BaseModel):
    device_id: str
    student_id: str
    base_snapshot_version: int | None = None
    last_server_cursor: str | None = None
    content_pack_versions: list[str] = []
    events: list[SyncEventEnvelope] = Field(default_factory=list)


class SyncResponse(BaseModel):
    accepted_event_ids: list[str] = []
    duplicate_event_ids: list[str] = []
    rejected_events: list[SyncRejectedEvent] = []
    conflicts: list[SyncConflict] = []
    new_snapshot_version: int
    new_server_cursor: str
    server_events: list[dict] = []
    required_content_packs: list[str] = []
    memory_snapshot: dict = {}
    sync_status: str = "complete"


class DeviceRegistration(BaseModel):
    device_id: str
    student_id: str
    status: str


class DeviceRevokeRequest(BaseModel):
    student_id: str


class SnapshotResponse(BaseModel):
    student: dict
    skill_states: list[dict]
    session: dict | None = None
    plan: dict | None = None
    strategy_memory: dict = {}
    content_pack_versions: list[str] = []
    snapshot_version: int
    server_cursor: str
