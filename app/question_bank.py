from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

from .models import Question, Skill

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def packs_root() -> Path:
    return Path(os.getenv("BRIDGESAT_PACKS_ROOT", PROJECT_ROOT / "content" / "packs"))


class ContentPackError(RuntimeError):
    pass


@lru_cache(maxsize=16)
def _read_pack_directory(pack_dir: Path) -> list[Question]:
    manifest_path = pack_dir / "manifest.json"
    if not manifest_path.is_file():
        raise ContentPackError(f"Pack {pack_dir.name} is missing manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("status") != "published":
        raise ContentPackError(f"Pack {pack_dir.name} status is not 'published'")
    allowed_versions = manifest.get("allowed_item_schema_versions", ["v1"])
    items_path = pack_dir / "items.jsonl"
    if not items_path.is_file():
        return []
    questions: list[Question] = []
    for line in items_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        if item.get("schema_version") not in allowed_versions:
            continue
        questions.append(_to_question(item))
    return questions


def _to_question(item: dict) -> Question:
    """Translate a v1 content-pack item into the student-facing Question."""
    if "answer" in item and isinstance(item.get("choices", []) and item["choices"][0], str):
        return Question.model_validate(item)
    choice_texts = [choice["text"] for choice in item["choices"]]
    answer_text = next(
        choice["text"] for choice in item["choices"] if choice["id"] == item["answer_choice_id"]
    )
    return Question(
        id=item["id"],
        skill=Skill(item["target_skill"]),
        difficulty=item["difficulty"],
        prompt=item["prompt"],
        choices=choice_texts,
        answer=answer_text,
        hints=[hint["text"] for hint in item["hints"]],
        explanation=item["worked_explanation"],
    )


@lru_cache(maxsize=4)
def _load_all(root: Path) -> list[Question]:
    if not root.is_dir():
        return []
    questions: list[Question] = []
    for pack_dir in sorted(root.iterdir()):
        if not pack_dir.is_dir():
            continue
        try:
            questions.extend(_read_pack_directory(pack_dir))
        except ContentPackError:
            continue
    return questions


def load_questions() -> list[Question]:
    """Load questions only from published content packs.

    This is the production path. Quarantined starter content, drafts, and
    unpublished packs are never returned.
    """
    return _load_all(packs_root())


def clear_cache() -> None:
    _load_all.cache_clear()
    _read_pack_directory.cache_clear()


def question_map() -> dict[str, Question]:
    return {question.id: question for question in load_questions()}