"""Version-bound answer scoring (SYNC_PROTOCOL section 6.7).

An offline ANSWER_SUBMITTED event references `question_id` +
`question_version` + `content_pack_version`. The server scores against the
exact referenced version's answer key, never a newer one. When the pack
version or the question version is unknown, the event is rejected with
QUESTION_VERSION_UNKNOWN.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.question_bank import packs_root

from .protocol import SyncErrorCode


class QuestionVersionError(RuntimeError):
    """Raised when the referenced question version cannot be scored."""

    def __init__(self, code: SyncErrorCode, message: str) -> None:
        self.code = code
        super().__init__(message)


class PackAnswerKey:
    """Answer key for one published content pack version.

    The key is immutable once built from a published pack directory: scoring
    an attempt always uses the answer key of the exact referenced version.
    """

    def __init__(self, pack_version: str, pack_dir: Path) -> None:
        self.pack_version = pack_version
        self.pack_dir = pack_dir
        self._items: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        items_path = self.pack_dir / "items.jsonl"
        if not items_path.exists():
            raise QuestionVersionError(
                SyncErrorCode.QUESTION_VERSION_UNKNOWN,
                f"Pack {self.pack_version} has no items.jsonl",
            )
        for line in items_path.read_text().splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            self._items[item["id"]] = item

    def answer_choice_id(self, question_id: str, question_version: int) -> str:
        item = self._items.get(question_id)
        if item is None:
            raise QuestionVersionError(
                SyncErrorCode.QUESTION_VERSION_UNKNOWN,
                f"Question {question_id} not found in pack {self.pack_version}",
            )
        if item.get("version") != question_version:
            raise QuestionVersionError(
                SyncErrorCode.QUESTION_VERSION_UNKNOWN,
                f"Question {question_id} version {question_version} unknown in "
                f"pack {self.pack_version} (available: {item.get('version')})",
            )
        return item["answer_choice_id"]

    def item_meta(self, question_id: str, question_version: int) -> dict:
        item = self._items.get(question_id)
        if item is None:
            raise QuestionVersionError(
                SyncErrorCode.QUESTION_VERSION_UNKNOWN,
                f"Question {question_id} not found in pack {self.pack_version}",
            )
        if item.get("version") != question_version:
            raise QuestionVersionError(
                SyncErrorCode.QUESTION_VERSION_UNKNOWN,
                f"Question {question_id} version {question_version} unknown in "
                f"pack {self.pack_version} (available: {item.get('version')})",
            )
        return {
            "skill": item.get("target_skill"),
            "subskill": item.get("target_subskill"),
            "difficulty": item.get("difficulty"),
            "misconception_map": item.get("misconception_map", {}),
            "hints": item.get("hints", []),
        }


class VersionedAnswerKey:
    """Registry of published packs, keyed by pack version string."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or packs_root()
        self._packs: dict[str, PackAnswerKey] = {}

    def _available_versions(self) -> dict[str, Path]:
        versions: dict[str, Path] = {}
        for pack_dir in sorted(self.root.iterdir()):
            if not pack_dir.is_dir():
                continue
            manifest_path = pack_dir / "manifest.json"
            if not manifest_path.exists():
                continue
            manifest = json.loads(manifest_path.read_text())
            if manifest.get("status") != "published":
                continue
            pack_version = manifest.get("pack_version") or pack_dir.name
            versions[pack_version] = pack_dir
        return versions

    def list_versions(self) -> list[str]:
        return sorted(self._available_versions().keys())

    def pack(self, pack_version: str) -> PackAnswerKey:
        if pack_version in self._packs:
            return self._packs[pack_version]
        available = self._available_versions()
        pack_dir = available.get(pack_version)
        if pack_dir is None:
            raise QuestionVersionError(
                SyncErrorCode.QUESTION_VERSION_UNKNOWN,
                f"Content pack {pack_version} is not available on this server",
            )
        key = PackAnswerKey(pack_version, pack_dir)
        self._packs[pack_version] = key
        return key

    def score(
        self,
        *,
        pack_version: str,
        question_id: str,
        question_version: int,
        selected_choice_id: str,
    ) -> bool:
        key = self.pack(pack_version)
        answer = key.answer_choice_id(question_id, question_version)
        return selected_choice_id == answer
