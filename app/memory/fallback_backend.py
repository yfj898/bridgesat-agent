"""Fallback student memory (ARCHITECTURE §8.6, COMPETITION plan §10).

Retrieval chain: Mnemis within a strict timeout -> authoritative PostgreSQL
episodic/aggregate queries -> offline client snapshot. PostgreSQL is always
available to the request and the loop never depends on Mnemis success; every
call records route and latency so the fallback is measurable
(``memory_fallback_rate``).
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

import psycopg

from app.memory.mnemis_backend import (
    MnemisMemoryAdapter,
    MnemisMemoryResult,
    SYSTEM_1_TIMEOUT_MS,
)
from app.memory.pg_memory import PGMemory, UNSET_MISCONCEPTION


@dataclass
class RecallHit:
    episode_id: str
    memory_id: str | None
    retrieval_route: str
    confidence: float
    supporting_episode_ids: list[str]
    retrieval_score: float = 0.0

    @classmethod
    def from_mnemis(cls, result: MnemisMemoryResult | dict) -> "RecallHit":
        if isinstance(result, dict):
            supporting = list(result.get("supporting_episode_ids") or [])
            episode_id = (
                supporting[0] if supporting else result.get("memory_id", "unknown")
            )
            return cls(
                episode_id=episode_id,
                memory_id=result.get("memory_id"),
                retrieval_route=result.get("retrieval_route", "mnemis_system1"),
                confidence=float(result.get("confidence", 0.0)),
                supporting_episode_ids=supporting,
                retrieval_score=float(result.get("retrieval_score", 0.0)),
            )
        return cls(
            episode_id=result.supporting_episode_ids[0],
            memory_id=result.memory_id,
            retrieval_route=result.retrieval_route,
            confidence=result.confidence,
            supporting_episode_ids=result.supporting_episode_ids,
            retrieval_score=result.retrieval_score,
        )


@dataclass
class RecallResult:
    route: str
    hits: list[RecallHit]


class FallbackStudentMemory:
    """Retrieval facade over the authoritative PostgreSQL store with an optional
    Mnemis index in front. Writes never pass through here: episodes and facts
    are written to PostgreSQL and delivered to Mnemis via the transactional
    outbox."""

    def __init__(
        self,
        connection: psycopg.Connection,
        mnemis: MnemisMemoryAdapter | Any | None = None,
        *,
        timeout_ms: int | None = None,
        offline_snapshot: Any | None = None,
    ) -> None:
        self.connection = connection
        self.pg = PGMemory(connection)
        self.mnemis = mnemis
        # The recall budget must fit the index behind the adapter: the
        # LLM-backed index (NvidiaMemoryIndex) needs the LLM round-trip time,
        # not the 800 ms HTTP budget. Prefer an explicit timeout, then the
        # adapter's own, then the legacy default.
        if timeout_ms is not None:
            self.timeout_ms = timeout_ms
        elif hasattr(mnemis, "timeout_ms") and mnemis.timeout_ms is not None:
            self.timeout_ms = mnemis.timeout_ms
        else:
            self.timeout_ms = SYSTEM_1_TIMEOUT_MS
        self.offline_snapshot = offline_snapshot
        self._route_counts: dict[str, int] = {}
        self._latencies: deque[float] = deque(maxlen=200)

    async def recall_similar(
        self,
        *,
        student_id: str,
        skill: str,
        misconception: str | None | object = UNSET_MISCONCEPTION,
        limit: int = 5,
    ) -> RecallResult:
        started = time.perf_counter()
        hits: list[RecallHit] = []
        route = "pg"

        if self.mnemis is not None and misconception is not None:
            try:
                query = {
                    "student_id": student_id,
                    "skill": skill,
                }
                if misconception is not UNSET_MISCONCEPTION:
                    query["misconception"] = misconception
                results = await asyncio.wait_for(
                    self.mnemis.recall_similar(query),
                    timeout=self.timeout_ms / 1000,
                )
                hits = [
                    RecallHit.from_mnemis(r)
                    for r in self._scoped_mnemis_results(results, student_id)
                ]
                route = "mnemis_system1"
            except Exception:
                # Any ordinary adapter failure degrades to authoritative PG.
                # Do not catch BaseException: cancellation must propagate.
                hits = []

        if not hits:
            pg_kwargs = {
                "student_id": student_id,
                "skill": skill,
                "limit": limit,
            }
            if misconception is not UNSET_MISCONCEPTION:
                pg_kwargs["misconception"] = misconception
            episodes = self.pg.recall_episodes(**pg_kwargs)
            hits = [
                RecallHit(
                    episode_id=e.episode_id,
                    memory_id=None,
                    retrieval_route="pg",
                    confidence=e.confidence,
                    supporting_episode_ids=[e.episode_id],
                )
                for e in episodes
            ]
            route = "pg"

        if not hits and self.offline_snapshot is not None:
            snapshot_kwargs = {
                "student_id": student_id,
                "skill": skill,
                "limit": limit,
            }
            if misconception is not UNSET_MISCONCEPTION:
                snapshot_kwargs["misconception"] = misconception
            episodes = self.offline_snapshot.recall_episodes(**snapshot_kwargs)
            if misconception is not UNSET_MISCONCEPTION:
                episodes = [
                    episode
                    for episode in episodes
                    if episode.misconception == misconception
                ]
            hits = [
                RecallHit(
                    episode_id=e.episode_id,
                    memory_id=None,
                    retrieval_route="offline_snapshot",
                    confidence=e.confidence,
                    supporting_episode_ids=[e.episode_id],
                )
                for e in episodes
            ]
            route = "offline_snapshot"

        self._route_counts[route] = self._route_counts.get(route, 0) + 1
        self._latencies.append((time.perf_counter() - started) * 1000)
        return RecallResult(route=route, hits=hits)

    def recall_metrics(self) -> dict:
        total = sum(self._route_counts.values()) or 0
        mnemis_ok = self._route_counts.get("mnemis_system1", 0)
        return {
            "memory_fallback_rate": 0.0 if total == 0 else (total - mnemis_ok) / total,
            "memory_route_counts": dict(self._route_counts),
            "memory_latency_ms": {
                "avg": round(sum(self._latencies) / len(self._latencies), 2)
                if self._latencies
                else 0.0,
                "p95": self._latency_p95(),
            },
        }

    def _scoped_mnemis_results(
        self, results: list[Any], student_id: str
    ) -> list[Any]:
        """Retain only Mnemis results whose supporting episode IDs are a
        non-empty subset of the student's validated PostgreSQL episodes."""
        if not results:
            return []
        validated = self.pg.validated_episode_ids(student_id)
        if not validated:
            return []
        scoped: list[Any] = []
        for result in results:
            if isinstance(result, dict):
                supporting = list(result.get("supporting_episode_ids") or [])
            else:
                supporting = list(getattr(result, "supporting_episode_ids", None) or [])
            if supporting and set(supporting) <= validated:
                scoped.append(result)
        return scoped

    def _latency_p95(self) -> float:
        if not self._latencies:
            return 0.0
        ordered = sorted(self._latencies)
        return round(ordered[int(0.95 * (len(ordered) - 1))], 2)
