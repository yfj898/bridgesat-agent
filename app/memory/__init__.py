from __future__ import annotations

import os
from enum import StrEnum
from pathlib import Path

from .episode_builder import EpisodeBuilder
from .sqlite_backend import SQLiteMemory


class MemoryMode(StrEnum):
    LOCAL = "local"
    ENHANCED = "enhanced"


def memory_mode() -> MemoryMode:
    return MemoryMode(os.getenv("BRIDGESAT_MODE", MemoryMode.LOCAL.value))


class MemoryProvider:
    """Facade over the authoritative SQLite memory plus optional Mnemis.

    In MVP phase 1 only SQLite exists; Mnemis is an enhanced-mode adapter added
    later. The provider always exposes recall_episodes so the policy can query
    learner memory regardless of mode.
    """

    def __init__(self, database_path: Path) -> None:
        self.sqlite = SQLiteMemory(database_path)
        self.episodes = EpisodeBuilder(database_path)
        self.mode = memory_mode()
        self.mnemis = None

    def recall_episodes(
        self,
        *,
        student_id: str,
        skill: str,
        misconception: str | None = None,
        limit: int = 5,
    ):
        return self.sqlite.recall_episodes(
            student_id=student_id,
            skill=skill,
            misconception=misconception,
            limit=limit,
        )

    def has_successful_episode(self, *, student_id: str, skill: str, misconception: str | None) -> bool:
        return self.episodes.has_successful_episode(
            student_id=student_id, skill=skill, misconception=misconception
        )
