from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from .auth import TokenStore, require_student
from .engine import adapt, score_diagnostic
from .infrastructure.migration_runner import apply_migrations
from .knowledge.router import router as knowledge_router
from .memory.worker import OutboxWorker
from .models import (
    AdaptRequest,
    AdaptResponse,
    DiagnosticRequest,
    DiagnosticResponse,
    Question,
    Student,
    StudentCreate,
    StudentWithToken,
)
from .question_bank import load_questions
from .repository import StudentRepository
from .sync.content_packs import router as content_packs_router
from .sync.router import router as sync_router


ROOT = Path(__file__).resolve().parents[1]
DATABASE_PATH = Path(os.getenv("BRIDGESAT_DB", ROOT / "data" / "bridgesat.db"))

# Migrations run before the legacy repository touches the schema so the
# two can never disagree (migration 0003 extends `students` in place).
apply_migrations(DATABASE_PATH)

repository = StudentRepository(DATABASE_PATH)
token_store = TokenStore(DATABASE_PATH)

_llm_client = None


def _get_llm_client():
    """Lazily built LLM client for the route layer. Without
    BRIDGESAT_LLM_API_KEY the client is None and every route behaves exactly
    as the deterministic engine did before the LLM layer existed."""
    global _llm_client
    if _llm_client is None and os.getenv("BRIDGESAT_LLM_API_KEY"):
        from .agent.llm_client import LLMClient

        _llm_client = LLMClient()
    return _llm_client


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.memory import MemoryMode, memory_mode

    worker = OutboxWorker(DATABASE_PATH)
    mode = memory_mode()
    if mode == MemoryMode.ENHANCED:
        from app.memory import build_mnemis_index

        worker = OutboxWorker(DATABASE_PATH, index=build_mnemis_index(DATABASE_PATH))
    app.state.memory_worker = worker

    async def drain_loop() -> None:
        # Poll interval is exercised by the ablation/rebuild scripts, which
        # call run_pending in a drain loop themselves (MEMORY_CONSISTENCY
        # section 4). The interval here is generous so a local demo never
        # spins the loop pointlessly while enhanced-mode delivery still
        # proceeds.
        while True:
            try:
                await worker.run_pending_async()
            except Exception:
                pass
            await asyncio.sleep(2.0)

    if mode == MemoryMode.ENHANCED:
        task = asyncio.create_task(drain_loop())
        app.state.memory_worker_task = task
    else:
        app.state.memory_worker_task = None
    yield
    task = app.state.memory_worker_task
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    app.state.memory_worker = None
    app.state.memory_worker_task = None


app = FastAPI(
    title="BridgeSAT Agent",
    version="0.1.0",
    description="Offline-first adaptive SAT learning agent API.",
    lifespan=lifespan,
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


@app.post("/v1/students", response_model=StudentWithToken, status_code=201)
def create_student(payload: StudentCreate) -> StudentWithToken:
    student = repository.create(payload)
    token = token_store.issue(student.id)
    return StudentWithToken(**student.model_dump(), token=token)


@app.post("/v1/diagnostics", response_model=DiagnosticResponse)
def run_diagnostic(
    payload: DiagnosticRequest,
    student_id: str = Depends(require_student),
) -> DiagnosticResponse:
    student = repository.get(student_id)
    if student is None:
        raise HTTPException(status_code=404, detail="Student not found")
    try:
        result = score_diagnostic(student, payload.answers)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    repository.update_mastery(student.id, result.mastery)
    return result


@app.post("/v1/adapt", response_model=AdaptResponse)
def adapt_session(
    payload: AdaptRequest,
    student_id: str = Depends(require_student),
) -> AdaptResponse:
    student = repository.get(student_id)
    if student is None:
        raise HTTPException(status_code=404, detail="Student not found")
    previous = student.mastery.get(payload.skill, 0.5)
    result = adapt(previous, payload, llm=_get_llm_client())
    student.mastery[payload.skill] = result.mastery
    repository.update_mastery(student.id, student.mastery)
    return result


app.include_router(knowledge_router)
app.include_router(sync_router)
app.include_router(content_packs_router)


WEB_DIR = ROOT / "web"
app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
