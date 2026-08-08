"""LLM-backed local memory index with a Mnemis-compatible transport surface.

NvidiaMemoryIndex is a drop-in ``request(method, path, body, timeout_ms)``
transport for MnemisMemoryAdapter (see mnemis_backend.py): it implements the
same HTTP paths (/index/upsert, /recall/similar, /student/delete, /health)
but persists indexed episodes and facts in local SQLite and uses an injected
LLMClient for two things:

* upsert -- distills each episode into a compact retrieval summary;
* recall -- reranks locally selected candidates into a scored result list.

Degradation contract mirrors the rest of the memory chain: LLM failure on
upsert stores the payload summary as-is (indexing must not block), LLM
failure on recall raises MnemisUnavailableError so the caller falls back to
the authoritative SQLite recall. A missing LLM is a degraded index, not a
broken one.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from app.infrastructure.database import connect, transaction
from app.memory.mnemis_backend import MnemisUnavailableError

SUMMARY_PROMPT = (
    "You are the indexing layer of an SAT math tutor. Distill the following "
    "episode into a retrieval summary: the skill, what the student struggled "
    "with, which intervention was applied, and how it resolved. Keep it under "
    "40 words. Respond with the summary text only."
)

RERANK_PROMPT = (
    "You are the recall layer of an SAT math tutor. The student has a current "
    "state and a list of candidate memories. Reorder the candidates by "
    "relevance to the current state and score each 0..1. Respond with JSON "
    'only, an array: [{{"memory_id": "...", "confidence": 0.9, '
    '"retrieval_score": 0.95}}]. Do not invent memory_ids.'
)


class NvidiaMemoryIndex:
    """Local SQLite index over episodes/facts with LLM summaries and rerank.

    Implements the Mnemis transport surface so it can be injected as the
    ``transport`` of a MnemisMemoryAdapter without changing callers.
    """

    def __init__(
        self,
        database_path: Path,
        llm: Any | None = None,
        *,
        top_k: int = 5,
        candidate_pool: int = 15,
    ) -> None:
        self.db = database_path
        self.llm = llm
        self.top_k = top_k
        self.candidate_pool = candidate_pool
        self._init_schema()

    # ---------- transport surface (Mnemis-compatible) ----------

    async def request(
        self, method: str, path: str, body: dict, timeout_ms: int = 800
    ) -> dict:
        started = time.perf_counter()
        if method == "GET" and path.rstrip("/").endswith("/health"):
            return {"status": "ok" if self.llm is not None else "degraded"}
        if method != "POST":
            raise MnemisUnavailableError(f"unsupported method {method}")
        if path.rstrip("/").endswith("/index/upsert"):
            await self._handle_upsert(body, timeout_ms=timeout_ms)
            return {"ok": True}
        if path.rstrip("/").endswith("/recall/similar"):
            return await self._handle_recall(body, timeout_ms=timeout_ms)
        if path.rstrip("/").endswith("/student/delete"):
            await self._handle_delete(body)
            return {"deleted": True}
        raise MnemisUnavailableError(f"unknown path {path}")

    # ---------- upsert ----------

    async def _handle_upsert(self, body: dict, *, timeout_ms: int) -> None:
        payload = body.get("payload") or {}
        memory_type = str(body.get("type") or payload.get("type") or "episode")
        idempotency_key = str(body.get("idempotency_key") or "")
        summary = str(payload.get("summary") or "")
        budget = max(timeout_ms, self._llm_timeout_ms())
        if self.llm is not None:
            try:
                distilled = await asyncio.wait_for(
                    self.llm.complete(
                        SUMMARY_PROMPT
                        + "\n\nEpisode: "
                        + json.dumps(payload, sort_keys=True, default=str),
                        max_tokens=80,
                        temperature=0.0,
                        timeout_ms=budget,
                    ),
                    timeout=max(budget / 1000, 0.1),
                )
                if distilled and distilled.strip():
                    summary = distilled.strip()
            except (MnemisUnavailableError, TimeoutError, asyncio.TimeoutError):
                pass
        memory_id = str(
            payload.get("episode_id") or payload.get("fact_id") or payload.get("key")
            or idempotency_key
        )
        with connect(self.db) as connection:
            with transaction(connection):
                connection.execute(
                    "INSERT OR IGNORE INTO nvidia_memory_index ("
                    " idempotency_key, memory_id, memory_type, student_id, "
                    " skill, misconception, payload_json, summary, created_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        idempotency_key,
                        memory_id,
                        memory_type,
                        str(payload.get("student_id") or ""),
                        str(payload.get("skill") or ""),
                        str(payload.get("misconception") or ""),
                        json.dumps(payload, sort_keys=True, default=str),
                        summary,
                        _utc_now(),
                    ),
                )

    # ---------- recall ----------

    async def _handle_recall(self, body: dict, *, timeout_ms: int) -> dict:
        query = body.get("query") or {}
        top_k = int(body.get("top_k") or self.top_k)
        student_id = str(query.get("student_id") or "")
        skill = str(query.get("skill") or "")
        misconception = str(query.get("misconception") or "")

        candidates = self._select_candidates(
            student_id=student_id, skill=skill, misconception=misconception
        )
        if not candidates:
            return {"results": []}
        if self.llm is None:
            raise MnemisUnavailableError("LLM not configured for recall")
        ranked = await self._rerank_with_llm(candidates, query, timeout_ms=timeout_ms)
        ranked = ranked[:top_k]
        results = []
        for memory_id, confidence, score in ranked:
            row = next((c for c in candidates if c["memory_id"] == memory_id), None)
            if row is None:
                continue
            supporting = (
                [memory_id]
                if row["memory_type"] == "episode"
                else list(row.get("episode_ids") or [memory_id])
            )
            results.append(
                {
                    "memory_id": memory_id,
                    "memory_type": row["memory_type"],
                    "supporting_episode_ids": supporting,
                    "confidence": float(confidence),
                    "retrieval_score": float(score),
                }
            )
        return {"results": results}

    async def _rerank_with_llm(
        self, candidates: list[dict], query: dict, *, timeout_ms: int
    ) -> list[tuple[str, float, float]]:
        budget = max(timeout_ms, self._llm_timeout_ms())
        brief = [
            {
                "memory_id": c["memory_id"],
                "skill": c["skill"],
                "misconception": c["misconception"],
                "summary": c["summary"],
            }
            for c in candidates
        ]
        prompt = (
            RERANK_PROMPT
            + "\n\nCurrent state: "
            + json.dumps(query, sort_keys=True, default=str)
            + "\n\nCandidates: "
            + json.dumps(brief, sort_keys=True, default=str)
        )
        try:
            content = await asyncio.wait_for(
                self.llm.complete(
                    prompt,
                    max_tokens=200,
                    temperature=0.0,
                    timeout_ms=budget,
                ),
                timeout=max(budget / 1000, 0.1),
            )
        except (MnemisUnavailableError, TimeoutError, asyncio.TimeoutError):
            raise MnemisUnavailableError("LLM recall failed")
        try:
            parsed = json.loads(content.strip())
        except (ValueError, AttributeError):
            raise MnemisUnavailableError("LLM recall returned non-JSON")
        if not isinstance(parsed, list):
            raise MnemisUnavailableError("LLM recall returned non-array JSON")
        ranked: list[tuple[str, float, float]] = []
        known = {c["memory_id"] for c in candidates}
        for item in parsed:
            if isinstance(item, str):
                ranked.append((item, 0.6, 0.5))
            elif isinstance(item, dict):
                memory_id = str(item.get("memory_id") or "")
                if memory_id in known:
                    ranked.append(
                        (
                            memory_id,
                            float(item.get("confidence") or 0.0),
                            float(item.get("retrieval_score") or 0.0),
                        )
                    )
        for memory_id in known - {r[0] for r in ranked}:
            ranked.append((memory_id, 0.0, 0.0))
        ranked.sort(key=lambda r: r[2], reverse=True)
        return ranked

    # ---------- delete ----------

    async def _handle_delete(self, body: dict) -> None:
        student_id = str(body.get("student_id") or "")
        with connect(self.db) as connection:
            with transaction(connection):
                connection.execute(
                    "DELETE FROM nvidia_memory_index WHERE student_id = ?",
                    (student_id,),
                )

    # ---------- internals ----------

    def _llm_timeout_ms(self) -> int:
        """The LLM client's own budget, used when the adapter hands us the
        legacy 800 ms SYSTEM_1 budget that only fits an HTTP Mnemis service."""
        client_timeout = getattr(self.llm, "timeout_ms", None)
        return int(client_timeout) if client_timeout else 8000

    def _select_candidates(
        self, *, student_id: str, skill: str, misconception: str
    ) -> list[dict]:
        with connect(self.db) as connection:
            rows = connection.execute(
                "SELECT idempotency_key, memory_id, memory_type, skill, "
                " misconception, summary, payload_json FROM nvidia_memory_index"
                " WHERE student_id = ? AND (skill = ? OR skill LIKE ?)"
                " ORDER BY created_at DESC LIMIT ?",
                (student_id, skill, f"%{skill}%", self.candidate_pool),
            ).fetchall()
        candidates = []
        for row in rows:
            payload = {}
            try:
                payload = json.loads(row["payload_json"] or "{}")
            except ValueError:
                payload = {}
            if misconception and misconception not in (row["misconception"] or ""):
                continue
            candidates.append(
                {
                    "idempotency_key": row["idempotency_key"],
                    "memory_id": row["memory_id"],
                    "memory_type": row["memory_type"],
                    "skill": row["skill"],
                    "misconception": row["misconception"],
                    "summary": row["summary"],
                    "episode_ids": list(payload.get("episode_ids") or []),
                }
            )
        return candidates

    def _count_rows(self) -> int:
        with connect(self.db) as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM nvidia_memory_index"
            ).fetchone()
        return int(row[0])

    def _get_row(self, idempotency_key: str) -> sqlite3.Row | None:
        with connect(self.db) as connection:
            return connection.execute(
                "SELECT * FROM nvidia_memory_index WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()

    def _init_schema(self) -> None:
        with connect(self.db) as connection:
            with transaction(connection):
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS nvidia_memory_index ("
                    " idempotency_key TEXT PRIMARY KEY,"
                    " memory_id TEXT NOT NULL,"
                    " memory_type TEXT NOT NULL,"
                    " student_id TEXT NOT NULL,"
                    " skill TEXT NOT NULL DEFAULT '',"
                    " misconception TEXT NOT NULL DEFAULT '',"
                    " payload_json TEXT NOT NULL DEFAULT '{}',"
                    " summary TEXT NOT NULL DEFAULT '',"
                    " created_at TEXT NOT NULL"
                    ")"
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_nvidia_index_student"
                    " ON nvidia_memory_index (student_id, skill)"
                )


def _utc_now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()
