from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Callable, TypeVar

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from .auth import require_student
from .engine import adapt, score_diagnostic
from .infrastructure import pg
from .infrastructure.migration_runner import migrate_database
from .infrastructure.pg import transaction
from .knowledge.router import router as knowledge_router
from .memory import MemoryMode, build_mnemis_index, memory_mode
from .memory.outbox import student_advisory_lock
from .memory.tenant_dispatcher import TenantOutboxDispatcher
from .models import (
    AdaptRequest,
    AdaptResponse,
    DiagnosticRequest,
    DiagnosticResponse,
    Question,
    Skill,
    Student,
    StudentCreate,
    StudentWithToken,
)
from .question_bank import load_questions
from .repository import StudentRepository, StudentWriteRejectedError
from .request_context import (
    ConnectionFactory,
    TenantContextMiddleware,
    request_connection,
    request_token_store,
)
from .sync.content_packs import router as content_packs_router
from .sync.router import router as sync_router


ROOT = Path(__file__).resolve().parents[1]
WEB_DIR = ROOT / "web"

_llm_client = None
logger = logging.getLogger(__name__)
MasteryResult = TypeVar("MasteryResult")


def _get_llm_client():
    """Lazily build the optional LLM client for the route layer."""
    global _llm_client
    if _llm_client is None and os.getenv("BRIDGESAT_LLM_API_KEY"):
        from .agent.llm_client import LLMClient

        _llm_client = LLMClient()
    return _llm_client


@asynccontextmanager
async def lifespan(application: FastAPI):
    """Run privileged migrations at startup, not while importing the module."""
    mode = memory_mode()
    admin_connection = None
    dispatcher = None
    task = None
    application.state.memory_worker = None
    application.state.memory_dispatcher = None
    application.state.memory_worker_task = None

    try:
        if application.state.run_migrations:
            admin_connection = pg.connect_admin()
            app_probe = application.state.connection_factory()
            try:
                pg.assert_matching_database(admin_connection, app_probe)
            finally:
                pg.quiet_close(app_probe)
            migrate_database(admin_connection)
            if mode != MemoryMode.ENHANCED:
                try:
                    admin_connection.rollback()
                finally:
                    admin_connection.close()
                admin_connection = None

        if mode == MemoryMode.ENHANCED:
            if admin_connection is None:
                admin_connection = pg.connect_admin()
            dispatcher = TenantOutboxDispatcher(
                admin_connection,
                application.state.connection_factory,
                lambda connection, tenant_id: build_mnemis_index(connection),
            )
            application.state.memory_worker = dispatcher
            application.state.memory_dispatcher = dispatcher
            task = asyncio.create_task(_memory_drain_loop(dispatcher))
            application.state.memory_worker_task = task

        yield
    finally:
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        try:
            if dispatcher is not None:
                dispatcher.close()
            elif admin_connection is not None:
                try:
                    admin_connection.rollback()
                finally:
                    admin_connection.close()
        finally:
            application.state.memory_worker = None
            application.state.memory_dispatcher = None
            application.state.memory_worker_task = None


async def _memory_drain_loop(dispatcher: TenantOutboxDispatcher) -> None:
    """Poll the tenant dispatcher without allowing one transient poll error
    to kill the enhanced-mode task."""
    while True:
        try:
            await dispatcher.run_pending_async()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Memory outbox dispatcher poll failed; tenant_errors=%s",
                dispatcher.last_errors,
            )
        await asyncio.sleep(2.0)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Security defaults from THREAT_MODEL.md section 9."""

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'",
        )
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Permissions-Policy",
            "camera=(), microphone=(), geolocation=(), payment=()",
        )
        response.headers.setdefault("X-Frame-Options", "DENY")
        return response


def _repository_for(request: Request) -> StudentRepository:
    return StudentRepository(request_connection(request))


def _run_mastery_update(
    connection: psycopg.Connection,
    student_id: str,
    compute: Callable[[Student], tuple[MasteryResult, dict[Skill, float]]],
) -> MasteryResult:
    """Serialize the complete learner read/compute/write operation."""
    repository = StudentRepository(connection)
    with student_advisory_lock(connection, student_id):
        with transaction(connection):
            student = repository.get_for_update(student_id)
            if student is None:
                raise StudentWriteRejectedError(
                    f"Student {student_id} is not active in the current tenant"
                )
            result, mastery = compute(student)
            repository.update_mastery(student.id, mastery, commit=False)
            return result


def _diagnostic_mastery(
    student: Student, payload: DiagnosticRequest
) -> tuple[DiagnosticResponse, dict[Skill, float]]:
    result = score_diagnostic(student, payload.answers)
    return result, result.mastery


def _adapt_mastery(
    student: Student, payload: AdaptRequest
) -> tuple[AdaptResponse, dict[Skill, float]]:
    previous = student.mastery.get(payload.skill, 0.5)
    result = adapt(previous, payload, llm=_get_llm_client())
    student.mastery[payload.skill] = result.mastery
    return result, student.mastery


def create_app(
    connection_factory: Callable[[], psycopg.Connection] | None = None,
    *,
    run_migrations: bool = True,
) -> FastAPI:
    """Build an API app with request-scoped PostgreSQL dependencies."""
    factory: ConnectionFactory = (
        connection_factory if connection_factory is not None else pg.connect
    )
    application = FastAPI(
        title="BridgeSAT Agent",
        version="0.1.0",
        description="Offline-first adaptive SAT learning agent API.",
        lifespan=lifespan,
    )
    application.state.connection_factory = factory
    application.state.run_migrations = run_migrations

    application.add_middleware(SecurityHeadersMiddleware)
    application.add_middleware(
        TenantContextMiddleware,
        connection_factory=factory,
    )

    @application.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/v1/questions", response_model=list[Question])
    def questions() -> list[Question]:
        return load_questions()

    @application.post("/v1/students", response_model=StudentWithToken, status_code=201)
    def create_student(
        payload: StudentCreate,
        request: Request,
    ) -> StudentWithToken:
        student = _repository_for(request).create(payload)
        token = request_token_store(request).issue(student.id)
        return StudentWithToken(**student.model_dump(), token=token)

    @application.post("/v1/diagnostics", response_model=DiagnosticResponse)
    def run_diagnostic(
        payload: DiagnosticRequest,
        request: Request,
        student_id: str = Depends(require_student),
    ) -> DiagnosticResponse:
        try:
            result = _run_mastery_update(
                request_connection(request),
                student_id,
                lambda student: _diagnostic_mastery(student, payload),
            )
        except StudentWriteRejectedError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return result

    @application.post("/v1/adapt", response_model=AdaptResponse)
    def adapt_session(
        payload: AdaptRequest,
        request: Request,
        student_id: str = Depends(require_student),
    ) -> AdaptResponse:
        # Secondary/legacy path (Hybrid Integration Plan H0): the competition
        # PWA authority is /v1/sync/events -> SyncService. The web app never
        # calls /v1/adapt; it is retained for compatibility until usage is
        # characterized, then routed through the shared policy/gateway. It is
        # not the authoritative learner-state writer.
        try:
            result = _run_mastery_update(
                request_connection(request),
                student_id,
                lambda student: _adapt_mastery(student, payload),
            )
        except StudentWriteRejectedError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return result

    application.include_router(knowledge_router)
    application.include_router(sync_router)
    application.include_router(content_packs_router)
    application.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
    return application


app = create_app()
