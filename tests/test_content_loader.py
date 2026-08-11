import json
from pathlib import Path

import pytest

from app import question_bank


def _write_pack(
    root: Path,
    pack_id: str,
    *,
    status: str,
    items: list[dict],
    version: str = "0.1.0",
    catalog_id: str | None = None,
) -> Path:
    pack_dir = root / pack_id
    pack_dir.mkdir(parents=True, exist_ok=True)
    (pack_dir / "manifest.json").write_text(
        json.dumps(
            {
                "id": pack_id,
                "name": pack_id,
                "version": version,
                "pack_version": version,
                "pack_id": catalog_id or pack_id,
                "status": status,
                "allowed_item_schema_versions": ["v1"],
                "item_count": len(items),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    with (pack_dir / "items.jsonl").open("w", encoding="utf-8") as handle:
        for item in items:
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")
    return pack_dir


def _item(item_id: str) -> dict:
    return {
        "schema_version": "v1",
        "id": item_id,
        "skill": "linear_equations",
        "difficulty": 1,
        "prompt": "Prompt for " + item_id,
        "choices": ["1", "2", "3", "4"],
        "answer": "2",
        "hints": ["h1"],
        "explanation": "Explanation.",
    }


def test_loader_ignores_unpublished_packs(tmp_path: Path) -> None:
    _write_pack(
        tmp_path,
        "unpublished-pack",
        status="draft",
        items=[_item("draft-001")],
    )
    question_bank.clear_cache()

    assert question_bank._load_all(tmp_path) == []


def test_loader_accepts_only_published_packs(tmp_path: Path) -> None:
    _write_pack(
        tmp_path,
        "published-pack",
        status="published",
        items=[_item("pub-001")],
    )
    _write_pack(
        tmp_path,
        "unpublished-pack",
        status="approved",
        items=[_item("appr-001")],
    )
    question_bank.clear_cache()
    questions = question_bank._load_all(tmp_path)
    assert {item.id for item in questions} == {"pub-001"}


def test_student_loader_uses_latest_version_per_pack_id(tmp_path: Path) -> None:
    _write_pack(
        tmp_path,
        "math-0.1.0",
        catalog_id="math",
        version="0.1.0",
        status="published",
        items=[_item("old-001")],
    )
    _write_pack(
        tmp_path,
        "math-0.2.0",
        catalog_id="math",
        version="0.2.0",
        status="published",
        items=[_item("new-001")],
    )

    assert {item.id for item in question_bank._load_current(tmp_path)} == {"new-001"}
