from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from .engine import adapt, score_diagnostic
from .knowledge.router import router as knowledge_router
from .models import (
    AdaptRequest,
    AdaptResponse,
    DiagnosticRequest,
    DiagnosticResponse,
    Question,
    Student,
    StudentCreate,
)
from .question_bank import load_questions
from .repository import StudentRepository
from .sync.content_packs import router as content_packs_router
from .sync.router import router as sync_router


ROOT = Path(__file__).resolve().parents[1]
DATABASE_PATH = Path(os.getenv("BRIDGESAT_DB", ROOT / "data" / "bridgesat.db"))
repository = StudentRepository(DATABASE_PATH)

app = FastAPI(
    title="BridgeSAT Agent",
    version="0.1.0",
    description="Offline-first adaptive SAT learning agent API.",
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Security defaults from THREAT_MODEL.md section 9.

    Strict CSP, no-sniff, no-referrer, permissions policy, and frame
    denial. CORS is deliberately absent: the PWA is same-origin, so no
    cross-origin allowlist is needed and no wildcard can leak in.
    """

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers.setdefault("Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Permissions-Policy",
            "camera=(), microphone=(), geolocation=(), payment=()",
        )
        response.headers.setdefault("X-Frame-Options", "DENY")
        return response


app.add_middleware(SecurityHeadersMiddleware)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/v1/questions", response_model=list[Question])
def questions() -> list[Question]:
    return load_questions()


@app.post("/v1/students", response_model=Student, status_code=201)
def create_student(payload: StudentCreate) -> Student:
    return repository.create(payload)


@app.post("/v1/diagnostics", response_model=DiagnosticResponse)
def run_diagnostic(payload: DiagnosticRequest) -> DiagnosticResponse:
    student = repository.get(payload.student_id)
    if student is None:
        raise HTTPException(status_code=404, detail="Student not found")
    try:
        result = score_diagnostic(student, payload.answers)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    repository.update_mastery(student.id, result.mastery)
    return result


@app.post("/v1/adapt", response_model=AdaptResponse)
def adapt_session(payload: AdaptRequest) -> AdaptResponse:
    student = repository.get(payload.student_id)
    if student is None:
        raise HTTPException(status_code=404, detail="Student not found")
    previous = student.mastery.get(payload.skill, 0.5)
    result = adapt(previous, payload)
    student.mastery[payload.skill] = result.mastery
    repository.update_mastery(student.id, student.mastery)
    return result


app.include_router(knowledge_router)
app.include_router(sync_router)
app.include_router(content_packs_router)


WEB_DIR = ROOT / "web"
app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
