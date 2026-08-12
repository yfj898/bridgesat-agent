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
        self.pack_id = ""
        self._items: dict[str, dict] = {}
        self._lessons: list[dict] = []
        self._load()

    def _load(self) -> None:
        manifest_path = self.pack_dir / "manifest.json"
        if not manifest_path.exists():
            raise QuestionVersionError(
                SyncErrorCode.QUESTION_VERSION_UNKNOWN,
                f"Pack {self.pack_version} has no manifest.json",
            )
        manifest = json.loads(manifest_path.read_text())
        self.pack_id = manifest.get("pack_id") or self.pack_dir.name.rsplit("-", 1)[0]
        if manifest.get("pack_version") != self.pack_version:
            raise QuestionVersionError(
                SyncErrorCode.QUESTION_VERSION_UNKNOWN,
                f"Pack manifest version does not match {self.pack_version}",
            )
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
        lessons_path = self.pack_dir / "lessons.jsonl"
        if lessons_path.exists():
            self._lessons = [
                json.loads(line)
                for line in lessons_path.read_text().splitlines()
                if line.strip()
            ]

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
            "author_metadata": item.get("author_metadata", {}),
        }

    def teaching_asset_meta(
        self,
        skill: str,
        content_type: str,
        misconception: str | None = None,
    ) -> dict | None:
        """Return an approved lesson, preferring an exact misconception match."""
        candidates = sorted(
            (
                entry
                for entry in self._lessons
                if entry.get("content_type") == content_type
                and entry.get("target_skill") == skill
                and entry.get("review_status") == "approved"
            ),
            key=lambda entry: entry["id"],
        )
        lesson = next(
            (
                entry
                for entry in candidates
                if misconception
                and misconception in (entry.get("target_misconceptions") or [])
            ),
            candidates[0] if candidates else None,
        )
        if lesson is None:
            return None
        license_meta = lesson.get("license", {})
        lineage = lesson.get("source_lineage", {})
        return {
            "id": lesson["id"],
            "version": lesson.get("version"),
            "review_status": lesson["review_status"],
            "license": license_meta,
            "source_lineage": lineage,
            "content_type": lesson.get("content_type"),
            "target_misconceptions": lesson.get("target_misconceptions", []),
            # Additive registry facts (H4 shadow context): the Hybrid verifier
            # requires approved content to carry lineage and a content hash.
            "content_hash": lesson.get("content_hash", ""),
            "target_skill": lesson.get("target_skill"),
            "license_id": license_meta.get("id"),
            "license_name": license_meta.get("name"),
            "source_id": lineage.get("source_id"),
            "pack_id": self.pack_id,
            "pack_version": self.pack_version,
        }

    def worked_example_meta(
        self, skill: str, misconception: str | None = None
    ) -> dict | None:
        return self.teaching_asset_meta(skill, "worked_example", misconception)

    def micro_lesson_meta(
        self, skill: str, misconception: str | None = None
    ) -> dict | None:
        return self.teaching_asset_meta(skill, "micro_lesson", misconception)


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
