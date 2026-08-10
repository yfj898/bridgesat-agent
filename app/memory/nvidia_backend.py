"""LLM-backed derived memory index with a Mnemis-compatible transport.

``NvidiaMemoryIndex`` implements the Mnemis transport surface while keeping
PostgreSQL authoritative. Validated episodes and evidenced facts are written
to PostgreSQL before an outbox row is delivered here; recall always selects
from those tenant-scoped authoritative rows. Upsert and delete only maintain
transient derived delivery state, so an index outage or deletion cannot create
or remove learner memory.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import psycopg

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
    'only, an array: [{"memory_id": "...", "confidence": 0.9, '
    '"retrieval_score": 0.95}]. Do not invent memory_ids.'
)


class NvidiaMemoryIndex:
    """Derived LLM index over the current tenant's PostgreSQL memory rows.

    The small in-memory delivery cache is not a source of candidates or
    authoritative state. It only preserves idempotency and transient LLM
    summaries for the lifetime of this index instance.
    """

    def __init__(
        self,
        connection: psycopg.Connection,
        llm: Any | None = None,
        *,
        top_k: int = 5,
        candidate_pool: int = 15,
    ) -> None:
        self.connection = connection
        self.llm = llm
        self.top_k = top_k
        self.candidate_pool = candidate_pool
        self._derived_rows: dict[tuple[str, str], dict[str, Any]] = {}

    # ---------- transport surface (Mnemis-compatible) ----------

    async def request(
        self, method: str, path: str, body: dict, timeout_ms: int = 800
    ) -> dict:
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
        payload = dict(body.get("payload") or {})
        memory_type = str(body.get("type") or payload.get("type") or "episode")
        idempotency_key = str(body.get("idempotency_key") or "")
        tenant_id = self._current_tenant_id()
        memory_id = str(
            payload.get("episode_id")
            or payload.get("fact_id")
            or payload.get("key")
            or idempotency_key
        )
        record_key = idempotency_key or f"{memory_type}:{memory_id}"
        cache_key = (tenant_id, record_key)
        if cache_key in self._derived_rows:
            return

        summary = str(payload.get("summary") or payload.get("fact_text") or "")
        generated_summary: str | None = None
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
                    generated_summary = summary
            except Exception:
                # Derived summarization is best effort. The authoritative PG
                # row has already been committed and remains available.
                pass

        self._derived_rows[cache_key] = {
            "tenant_id": tenant_id,
            "idempotency_key": idempotency_key,
            "memory_id": memory_id,
            "memory_type": memory_type,
            "student_id": str(payload.get("student_id") or ""),
            "payload": payload,
            "summary": summary,
            "generated_summary": generated_summary,
        }

    # ---------- recall ----------

    async def _handle_recall(self, body: dict, *, timeout_ms: int) -> dict:
        query = body.get("query") or {}
        top_k = int(body.get("top_k") or self.top_k)
        student_id = str(query.get("student_id") or "")
        skill = str(query.get("skill") or "")
        misconception_value = query.get("misconception")
        misconception = str(misconception_value) if misconception_value else None

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
        tenant_id = self._current_tenant_id()
        student_id = str(query.get("student_id") or "")
        brief = [
            {
                "memory_id": c["memory_id"],
                "skill": c["skill"],
                "misconception": c["misconception"],
                "summary": self._candidate_summary(
                    tenant_id, student_id, c["memory_id"], c["summary"]
                ),
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
        except Exception as exc:
            raise MnemisUnavailableError("LLM recall failed") from exc
        try:
            parsed = json.loads(content.strip())
        except (ValueError, AttributeError, TypeError) as exc:
            raise MnemisUnavailableError("LLM recall returned non-JSON") from exc
        if not isinstance(parsed, list):
            raise MnemisUnavailableError("LLM recall returned non-array JSON")
        ranked: list[tuple[str, float, float]] = []
        known = {c["memory_id"] for c in candidates}
        for item in parsed:
            if isinstance(item, str) and item in known:
                ranked.append((item, 0.6, 0.5))
            elif isinstance(item, dict):
                memory_id = str(item.get("memory_id") or "")
                if memory_id in known:
                    try:
                        ranked.append(
                            (
                                memory_id,
                                float(item.get("confidence") or 0.0),
                                float(item.get("retrieval_score") or 0.0),
                            )
                        )
                    except (TypeError, ValueError) as exc:
                        raise MnemisUnavailableError(
                            "LLM recall returned invalid scores"
                        ) from exc
        for memory_id in known - {r[0] for r in ranked}:
            ranked.append((memory_id, 0.0, 0.0))
        ranked.sort(key=lambda r: r[2], reverse=True)
        return ranked

    # ---------- delete ----------

    async def _handle_delete(self, body: dict) -> None:
        student_id = str(body.get("student_id") or "")
        tenant_id = self._current_tenant_id()
        for cache_key, row in list(self._derived_rows.items()):
            if row["tenant_id"] == tenant_id and row["student_id"] == student_id:
                del self._derived_rows[cache_key]

    # ---------- internals ----------

    def _current_tenant_id(self) -> str:
        row = self.connection.execute(
            "SELECT current_setting('app.tenant_id', true) AS tenant_id"
        ).fetchone()
        tenant_id = row["tenant_id"] if row else None
        if not tenant_id:
            raise MnemisUnavailableError("app.tenant_id is not set")
        return str(tenant_id)

    def _derived_summary_for(
        self, tenant_id: str, student_id: str, memory_id: str
    ) -> str | None:
        for row in reversed(list(self._derived_rows.values())):
            if (
                row["tenant_id"] == tenant_id
                and row["student_id"] == student_id
                and row["memory_id"] == memory_id
                and row["generated_summary"] is not None
            ):
                return row["generated_summary"]
        return None

    def _candidate_summary(
        self, tenant_id: str, student_id: str, memory_id: str, pg_summary: str
    ) -> str:
        overlay = self._derived_summary_for(tenant_id, student_id, memory_id)
        return pg_summary if overlay is None else overlay

    def _llm_timeout_ms(self) -> int:
        """Use the LLM client's own budget when the adapter hands us the
        legacy 800 ms System-1 budget."""
        client_timeout = getattr(self.llm, "timeout_ms", None)
        return int(client_timeout) if client_timeout else 8000

    def _select_candidates(
        self, *, student_id: str, skill: str, misconception: str | None
    ) -> list[dict]:
        with self.connection.transaction():
            return self._select_candidates_in_transaction(
                student_id=student_id,
                skill=skill,
                misconception=misconception,
            )

    def _select_candidates_in_transaction(
        self, *, student_id: str, skill: str, misconception: str | None
    ) -> list[dict]:
        episode_clauses = [
            "tenant_id = current_setting('app.tenant_id')",
            "student_id = %s",
            "status = 'validated'",
            "(skill = %s OR skill LIKE %s)",
        ]
        episode_params: list[object] = [student_id, skill, f"%{skill}%"]
        if misconception:
            episode_clauses.append("misconception = %s")
            episode_params.append(misconception)
        episode_params.append(self.candidate_pool)
        episode_rows = self.connection.execute(
            f"""
            SELECT episode_id, skill, misconception, summary, confidence, created_at
            FROM learning_episodes
            WHERE {' AND '.join(episode_clauses)}
            ORDER BY created_at DESC
            LIMIT %s
            """,
            episode_params,
        ).fetchall()

        fact_clauses = [
            "tenant_id = current_setting('app.tenant_id')",
            "student_id = %s",
            "normalized_key LIKE %s",
        ]
        fact_params: list[object] = [student_id, f"{skill}\x1f%"]
        if misconception:
            fact_clauses.append("normalized_key LIKE %s")
            fact_params.append(f"{skill}\x1f{misconception}\x1f%")
        fact_params.append(self.candidate_pool)
        fact_rows = self.connection.execute(
            f"""
            SELECT fact_id, normalized_key, fact_text, confidence,
                   supporting_episode_ids_json, last_observed_at
            FROM student_memory_facts
            WHERE {' AND '.join(fact_clauses)}
            ORDER BY last_observed_at DESC
            LIMIT %s
            """,
            fact_params,
        ).fetchall()

        candidates: list[dict] = []
        for row in episode_rows:
            row_misconception = row["misconception"]
            if misconception and row_misconception != misconception:
                continue
            candidates.append(
                {
                    "memory_id": row["episode_id"],
                    "memory_type": "episode",
                    "skill": row["skill"],
                    "misconception": row_misconception,
                    "summary": row["summary"],
                    "confidence": row["confidence"],
                    "created_at": row["created_at"],
                    "episode_ids": [row["episode_id"]],
                }
            )

        for row in fact_rows:
            parts = str(row["normalized_key"] or "").split("\x1f", 2)
            if len(parts) != 3:
                continue
            fact_skill, fact_misconception, _ = parts
            if fact_skill != skill:
                continue
            if misconception and fact_misconception != misconception:
                continue
            try:
                episode_ids = json.loads(row["supporting_episode_ids_json"] or "[]")
            except (TypeError, ValueError):
                episode_ids = []
            if not isinstance(episode_ids, list):
                episode_ids = []
            candidates.append(
                {
                    "memory_id": row["fact_id"],
                    "memory_type": "fact",
                    "skill": fact_skill,
                    "misconception": fact_misconception or None,
                    "summary": row["fact_text"],
                    "confidence": row["confidence"],
                    "created_at": row["last_observed_at"],
                    "episode_ids": list(episode_ids or []),
                }
            )
        candidates.sort(key=lambda row: row["created_at"], reverse=True)
        return candidates[: self.candidate_pool]

    def _count_rows(self) -> int:
        """Return current-tenant derived deliveries, not authoritative rows."""
        tenant_id = self._current_tenant_id()
        return sum(
            row["tenant_id"] == tenant_id for row in self._derived_rows.values()
        )

    def _get_row(self, idempotency_key: str) -> dict | None:
        tenant_id = self._current_tenant_id()
        return self._derived_rows.get((tenant_id, idempotency_key))
