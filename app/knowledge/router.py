"""FastAPI router for governed knowledge retrieval."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from app.knowledge.local_backend import (
    DEFAULT_ALLOWED_LICENSES,
    DEFAULT_AUDIENCE,
    KnowledgeBackend,
    RetrievalResponse,
)
from app.request_context import request_connection


class RetrieveRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    audience: str = DEFAULT_AUDIENCE
    allowed_licenses: list[str] = list(DEFAULT_ALLOWED_LICENSES)
    skill: str | None = None
    subskill: str | None = None
    misconception: str | None = None
    difficulty: int | None = Field(default=None, ge=1, le=5)
    content_type: str | None = None
    recently_shown: list[str] = []
    max_results: int = Field(default=5, ge=1, le=20)


def get_backend(request: Request) -> KnowledgeBackend:
    return KnowledgeBackend(request_connection(request))


router = APIRouter(prefix="/v1/knowledge", tags=["knowledge"])


@router.post("/retrieve", response_model=RetrievalResponse)
def retrieve(
    payload: RetrieveRequest,
    backend: Annotated[KnowledgeBackend, Depends(get_backend)],
) -> RetrievalResponse:
    return backend.retrieve(
        payload.query,
        audience=payload.audience,
        allowed_licenses=tuple(payload.allowed_licenses),
        skill=payload.skill,
        subskill=payload.subskill,
        misconception=payload.misconception,
        difficulty=payload.difficulty,
        content_type=payload.content_type,
        recently_shown=set(payload.recently_shown),
        max_results=payload.max_results,
    )
