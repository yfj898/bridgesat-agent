from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles

from .engine import adapt, score_diagnostic
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


ROOT = Path(__file__).resolve().parents[1]
DATABASE_PATH = Path(os.getenv("BRIDGESAT_DB", ROOT / "data" / "bridgesat.db"))
repository = StudentRepository(DATABASE_PATH)

app = FastAPI(
    title="BridgeSAT Agent",
    version="0.1.0",
    description="Offline-first adaptive SAT learning agent API.",
)


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


WEB_DIR = ROOT / "web"
app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
