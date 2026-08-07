"""In-memory Mnemis-compatible index.

Implements the same async surface as ``MnemisMemoryAdapter`` (upsert_episode,
upsert_fact, recall_similar, global_select, delete_student, health) purely in
memory, keyed and filtered by student scope. Used for tests, demo parity
checks, and the memory ablation eval when no live Mnemis endpoint is
configured. It is a contract stub, never an authoritative store.
"""

from __future__ import annotations

from typing import Any


def _memory_id(student_id: str, memory_type: str, aggregate_id: str) -> str:
    return f"mem:{student_id}:{memory_type}:{aggregate_id}"


class InMemoryMnemisIndex:
    def __init__(self) -> None:
        self._episodes: dict[str, dict[str, dict]] = {}  # student_id -> memory_id -> doc
        self._facts: dict[str, dict[str, dict]] = {}
        self.upsert_keys_seen: set[str] = set()

    async def upsert_episode(self, payload: dict, idempotency_key: str) -> None:
        if idempotency_key in self.upsert_keys_seen:
            return
        self.upsert_keys_seen.add(idempotency_key)
        student_id = payload["student_id"]
        memory_id = _memory_id(student_id, "episode", payload["episode_id"])
        self._episodes.setdefault(student_id, {})[memory_id] = payload

    async def upsert_fact(self, payload: dict, idempotency_key: str) -> None:
        if idempotency_key in self.upsert_keys_seen:
            return
        self.upsert_keys_seen.add(idempotency_key)
        student_id = payload["student_id"]
        memory_id = _memory_id(student_id, "fact", payload["fact_id"])
        self._facts.setdefault(student_id, {})[memory_id] = payload

    async def recall_similar(
        self,
        query: dict,
        *,
        top_k: int = 5,
        min_confidence: float = 0.55,
    ) -> list[dict]:
        student_id = query.get("student_id")
        if not student_id:
            return []
        skill = query.get("skill")
        misconception = query.get("misconception")
        scored: list[tuple[float, str, dict]] = []
        for memory_id, doc in self._episodes.get(student_id, {}).items():
            score = 0.0
            if doc.get("skill") == skill:
                score += 0.6
            if misconception and doc.get("misconception") == misconception:
                score += 0.4
            if score == 0.0:
                continue
            scored.append(
                (score, memory_id, doc)
            )
        scored.sort(key=lambda item: item[0], reverse=True)
        results = []
        for score, memory_id, doc in scored[:top_k]:
            if doc.get("confidence", 1.0) < min_confidence:
                continue
            results.append(
                {
                    "memory_id": memory_id,
                    "memory_type": "episode",
                    "supporting_episode_ids": [doc["episode_id"]],
                    "confidence": doc.get("confidence", 1.0),
                    "retrieval_score": score,
                    "index_version": "mnemis-stub",
                }
            )
        return results

    async def global_select(self, query: dict, **kwargs: Any) -> list[dict]:
        return await self.recall_similar(query, **kwargs)

    async def delete_student(self, student_id: str, idempotency_key: str) -> None:
        if idempotency_key in self.upsert_keys_seen:
            return
        self.upsert_keys_seen.add(idempotency_key)
        self._episodes.pop(student_id, None)
        self._facts.pop(student_id, None)

    async def health(self) -> bool:
        return True

    async def count_episodes(self, student_id: str) -> int:
        return len(self._episodes.get(student_id, {}))

    async def count_facts(self, student_id: str) -> int:
        return len(self._facts.get(student_id, {}))

    async def total_episode_count(self) -> int:
        return sum(len(docs) for docs in self._episodes.values())

    def all_episode_ids(self, student_id: str) -> set[str]:
        return {
            doc["episode_id"] for doc in self._episodes.get(student_id, {}).values()
        }
