"""Mnemis memory adapter (MEMORY_CONSISTENCY §9.2, §9.3, §10).

Mnemis is an optional derived long-term-memory index. SQLite remains the
authoritative store. This adapter only ever carries validated episodes and
evidenced facts, never answers, mastery, state-machine or authoritative data.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

DEFAULT_BASE_URL = os.getenv("BRIDGESAT_MNEMIS_URL", "http://localhost:8010")
DEFAULT_API_KEY = os.getenv("BRIDGESAT_MNEMIS_API_KEY", "")
DEFAULT_TIMEOUT_MS = int(os.getenv("BRIDGESAT_MNEMIS_TIMEOUT_MS", "800"))
DEFAULT_TOP_K = 5
DEFAULT_MIN_CONFIDENCE = 0.55
INDEX_VERSION = "mnemis-0.1.0"

SYSTEM_1_TIMEOUT_MS = 800
SYSTEM_2_TIMEOUT_MS = 3000

Transport = Callable[..., Awaitable[dict]]


class MnemisUnavailableError(RuntimeError):
    """Mnemis timed out, errored, or is not configured."""


@dataclass(frozen=True)
class MnemisMemoryResult:
    memory_id: str
    memory_type: str
    supporting_episode_ids: list[str]
    confidence: float
    retrieval_route: str
    retrieval_score: float
    index_version: str


async def _unconfigured_transport(
    method: str, path: str, body: dict, timeout_ms: int
) -> dict:
    raise MnemisUnavailableError("Mnemis transport is not configured")


class MnemisMemoryAdapter:
    """HTTP adapter over a Mnemis-compatible index service.

    The transport is an async ``request(method, path, body, timeout_ms)``
    callable returning the parsed JSON response and raising on non-success or
    timeout. Injects for tests; defaults to an unavailable transport so
    enhanced mode degrades to SQLite when no Mnemis endpoint is configured.
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        transport: Transport | None = None,
        timeout_ms: int = SYSTEM_1_TIMEOUT_MS,
        top_k: int = DEFAULT_TOP_K,
        min_confidence: float = DEFAULT_MIN_CONFIDENCE,
        index_version: str = INDEX_VERSION,
    ) -> None:
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.api_key = api_key if api_key is not None else DEFAULT_API_KEY
        self._transport = transport or _unconfigured_transport
        self.timeout_ms = timeout_ms
        self.top_k = top_k
        self.min_confidence = min_confidence
        self.index_version = index_version

    # ---------- indexing ----------

    async def upsert_episode(self, episode: dict, idempotency_key: str) -> None:
        await self._request(
            "POST",
            "/index/upsert",
            {"type": "episode", "payload": episode, "idempotency_key": idempotency_key},
        )

    async def upsert_fact(self, fact: dict, idempotency_key: str) -> None:
        await self._request(
            "POST",
            "/index/upsert",
            {"type": "fact", "payload": fact, "idempotency_key": idempotency_key},
        )

    async def delete_student(self, student_id: str, idempotency_key: str) -> None:
        await self._request(
            "POST",
            "/student/delete",
            {"student_id": student_id, "idempotency_key": idempotency_key},
        )

    # ---------- retrieval ----------

    async def recall_similar(
        self, query: dict, *, timeout_ms: int | None = None
    ) -> list[MnemisMemoryResult]:
        response = await self._request(
            "POST",
            "/recall/similar",
            {"query": query, "top_k": self.top_k, "min_confidence": self.min_confidence},
            timeout_ms=timeout_ms or SYSTEM_1_TIMEOUT_MS,
        )
        return [
            result
            for result in self._shape_results(response.get("results", []), "mnemis_system1")
            if result.supporting_episode_ids and result.confidence >= self.min_confidence
        ]

    async def global_select(
        self, query: dict, *, timeout_ms: int = SYSTEM_2_TIMEOUT_MS
    ) -> list[MnemisMemoryResult]:
        response = await self._request(
            "POST",
            "/recall/global",
            {"query": query, "max_nodes": 12, "max_depth": 4},
            timeout_ms=timeout_ms,
        )
        return [
            result
            for result in self._shape_results(response.get("results", []), "mnemis_system2")
            if result.supporting_episode_ids
        ]

    async def health(self) -> bool:
        try:
            response = await self._request("GET", "/health", {}, timeout_ms=500)
            return response.get("status") == "ok"
        except MnemisUnavailableError:
            return False

    # ---------- internals ----------

    def _shape_results(
        self, raw: list[dict], route: str
    ) -> list[MnemisMemoryResult]:
        shaped: list[MnemisMemoryResult] = []
        for item in raw:
            try:
                shaped.append(
                    MnemisMemoryResult(
                        memory_id=str(item["memory_id"]),
                        memory_type=str(item["memory_type"]),
                        supporting_episode_ids=list(item.get("supporting_episode_ids") or []),
                        confidence=float(item["confidence"]),
                        retrieval_route=route,
                        retrieval_score=float(item.get("retrieval_score", 0.0)),
                        index_version=self.index_version,
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        shaped.sort(key=lambda r: r.retrieval_score, reverse=True)
        return shaped

    async def _request(
        self, method: str, path: str, body: dict, *, timeout_ms: int | None = None
    ) -> dict:
        started = time.perf_counter()
        try:
            request = (
                self._transport.request
                if hasattr(self._transport, "request")
                else self._transport
            )
            response = await request(
                method,
                f"{self.base_url}{path}",
                body,
                timeout_ms or self.timeout_ms,
            )
        except (TimeoutError, OSError) as exc:
            raise MnemisUnavailableError(f"Mnemis call failed: {exc}") from exc
        except MnemisUnavailableError:
            raise
        except Exception as exc:  # transport contract violation -> degrade
            raise MnemisUnavailableError(f"Mnemis call failed: {exc}") from exc
        elapsed = (time.perf_counter() - started) * 1000
        if not isinstance(response, dict):
            raise MnemisUnavailableError("Mnemis returned a non-JSON response")
        response.setdefault("_latency_ms", round(elapsed, 2))
        return response
