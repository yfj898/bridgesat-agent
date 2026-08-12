from __future__ import annotations

import os
from enum import StrEnum

import psycopg

from .episode_builder import EpisodeBuilder
from .pg_memory import PGMemory, UNSET_MISCONCEPTION


class MemoryMode(StrEnum):
    LOCAL = "local"
    ENHANCED = "enhanced"


def memory_mode() -> MemoryMode:
    return MemoryMode(os.getenv("BRIDGESAT_MODE", MemoryMode.LOCAL.value))


def build_mnemis_index(connection: psycopg.Connection):
    """Build the enhanced-mode index for the memory outbox worker.

    With ``BRIDGESAT_LLM_API_KEY`` configured, returns a MnemisMemoryAdapter
    over the local LLM-backed index (NvidiaMemoryIndex), which summarizes
    episodes and reranks recall through the OpenAI-compatible LLM endpoint.
    Without it, returns the default adapter whose transport is unavailable, so
    enhanced mode degrades to the authoritative PostgreSQL recall exactly as
    before. The index is only ever a derived store: PostgreSQL stays
    authoritative.
    """
    from .mnemis_backend import MnemisMemoryAdapter

    if not os.getenv("BRIDGESAT_LLM_API_KEY"):
        return MnemisMemoryAdapter()
    from app.agent.llm_client import LLMClient

    from .nvidia_backend import NvidiaMemoryIndex

    client = LLMClient()
    index = NvidiaMemoryIndex(connection, llm=client)
    return MnemisMemoryAdapter(
        base_url="http://local/nvidia-index",
        transport=index,
        # The LLM-backed index needs the LLM round-trip time, not the 800 ms
        # HTTP budget the default Mnemis adapter assumes; otherwise recall
        # would always time out and fall back to PostgreSQL.
        timeout_ms=client.timeout_ms,
    )


class MemoryProvider:
    """Facade over the authoritative PostgreSQL memory plus optional Mnemis.

    In MVP phase 1 only PostgreSQL exists; Mnemis is an enhanced-mode adapter
    added later. The provider always exposes recall_episodes so the policy
    can query learner memory regardless of mode.
    """

    def __init__(self, connection) -> None:
        self.pg = PGMemory(connection)
        self.episodes = EpisodeBuilder(connection)
        self.mode = memory_mode()
        self.mnemis = None

    def recall_episodes(
        self,
        *,
        student_id: str,
        skill: str,
        misconception: str | None | object = UNSET_MISCONCEPTION,
        limit: int = 5,
    ):
        kwargs = {
            "student_id": student_id,
            "skill": skill,
            "limit": limit,
        }
        if misconception is not UNSET_MISCONCEPTION:
            kwargs["misconception"] = misconception
        return self.pg.recall_episodes(
            **kwargs,
        )

    def has_successful_episode(self, *, student_id: str, skill: str, misconception: str | None) -> bool:
        return self.episodes.has_successful_episode(
            student_id=student_id, skill=skill, misconception=misconception
        )

    def recall_contradicting_episodes(
        self, *, student_id: str, skill: str, misconception: str | None, limit: int = 3
    ):
        return self.pg.recall_contradicting_episodes(
            student_id=student_id,
            skill=skill,
            misconception=misconception,
            limit=limit,
        )
