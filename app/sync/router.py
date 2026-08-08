"""FastAPI router for the offline synchronization protocol."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from app.auth import require_student
from app.sync.protocol import (
    DeviceRegistration,
    DeviceRevokeRequest,
    SnapshotResponse,
    SyncRequest,
    SyncResponse,
)
from app.sync.service import DeviceNotFoundError, DeviceRevokedError, SyncService
from app.sync.versioned_scoring import packs_root

router = APIRouter(prefix="/v1/sync", tags=["sync"])

_service: SyncService | None = None


def get_service() -> SyncService:
    global _service
    if _service is None:
        from app.main import DATABASE_PATH

        _service = SyncService(DATABASE_PATH)
    return _service


@router.post("/devices", response_model=DeviceRegistration, status_code=201)
def register_device(
    payload: dict,
    student_id: str = Depends(require_student),
) -> DeviceRegistration:
    try:
        return get_service().register_device(
            student_id, payload.get("device_name")
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete("/devices/{device_id}")
def revoke_device(
    device_id: str,
    payload: DeviceRevokeRequest,
    student_id: str = Depends(require_student),
) -> dict[str, str]:
    try:
        get_service().revoke_device(device_id, student_id)
    except DeviceNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"status": "revoked"}


@router.post("/events", response_model=SyncResponse)
def sync_events(
    payload: SyncRequest,
    student_id: str = Depends(require_student),
) -> SyncResponse:
    if payload.student_id != student_id:
        raise HTTPException(status_code=403, detail="Student scope mismatch")
    try:
        return get_service().process_batch(payload)
    except (DeviceNotFoundError, DeviceRevokedError) as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/snapshot", response_model=SnapshotResponse)
def sync_snapshot(student_id: str = Depends(require_student)) -> SnapshotResponse:
    try:
        return get_service().build_snapshot(student_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
