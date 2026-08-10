"""FastAPI router for the offline synchronization protocol."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.auth import require_student
from app.infrastructure.pg import transaction
from app.memory.outbox import student_advisory_lock
from app.request_context import request_connection
from app.sync.protocol import (
    DeviceRegistration,
    DeviceRevokeRequest,
    SnapshotResponse,
    SyncRequest,
    SyncResponse,
)
from app.sync.service import (
    DeviceNotFoundError,
    DeviceRevokedError,
    StudentInactiveError,
    SyncService,
)

router = APIRouter(prefix="/v1/sync", tags=["sync"])


def get_service(request: Request) -> SyncService:
    return SyncService(request_connection(request))


@router.post("/devices", response_model=DeviceRegistration, status_code=201)
def register_device(
    payload: dict,
    request: Request,
    student_id: str = Depends(require_student),
) -> DeviceRegistration:
    try:
        return get_service(request).register_device(
            student_id, payload.get("device_name")
        )
    except StudentInactiveError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete("/devices/{device_id}")
def revoke_device(
    device_id: str,
    payload: DeviceRevokeRequest,
    request: Request,
    student_id: str = Depends(require_student),
) -> dict[str, str]:
    try:
        get_service(request).revoke_device(device_id, student_id)
    except StudentInactiveError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except DeviceNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"status": "revoked"}


@router.post("/events", response_model=SyncResponse)
def sync_events(
    payload: SyncRequest,
    request: Request,
    student_id: str = Depends(require_student),
) -> SyncResponse:
    if payload.student_id != student_id:
        raise HTTPException(status_code=403, detail="Student scope mismatch")
    try:
        return get_service(request).process_batch(payload)
    except (DeviceNotFoundError, DeviceRevokedError) as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/snapshot", response_model=SnapshotResponse)
def sync_snapshot(
    request: Request,
    requested_student_id: str | None = Query(default=None, alias="student_id"),
    student_id: str = Depends(require_student),
) -> SnapshotResponse:
    if requested_student_id is not None and requested_student_id != student_id:
        raise HTTPException(status_code=403, detail="Student scope mismatch")
    try:
        service = get_service(request)
        with student_advisory_lock(service.connection, student_id):
            with transaction(service.connection):
                return service.build_snapshot(student_id, in_transaction=True)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
