from pathlib import Path

import pytest

from app.ingestion.registry import RegistryError, SourceRegistry
from app.ingestion.review import (
    age_precheck,
    deduplicate,
    license_precheck,
    map_skill,
)


ROOT = Path(__file__).resolve().parents[1]


def test_restricted_sources_cannot_be_acquired() -> None:
    registry = SourceRegistry(ROOT / "config" / "sources.yaml")
    registry.validate_restricted_sources()

    for source_id in ("college_board", "khan_academy", "openstax"):
        with pytest.raises(RegistryError):
            registry.acquire(source_id, "download")


def test_approved_machine_sources_allow_download() -> None:
    registry = SourceRegistry(ROOT / "config" / "sources.yaml")
    for source_id in (
        "deepmind_mathematics_dataset",
        "project_gutenberg",
        "library_of_congress_free_to_use",
        "gsm8k",
    ):
        assert registry.acquire(source_id, "download").id == source_id


def test_math_module_maps_to_frozen_skill() -> None:
    mapping = map_skill(
        {
            "source_id": "deepmind_mathematics_dataset",
            "upstream_module": "algebra__linear_1d",
        }
    )
    assert mapping["primary_skill"] == "linear_equations"
    assert mapping["mapping_confidence"] == 1.0


def test_exact_duplicates_are_removed() -> None:
    rows = [
        {
            "id": "a",
            "source_id": "deepmind_mathematics_dataset",
            "question": "Solve x + 1 = 2.",
            "answer": "1",
        },
        {
            "id": "b",
            "source_id": "deepmind_mathematics_dataset",
            "question": "Solve x + 1 = 2.",
            "answer": "1",
        },
    ]
    kept, duplicates = deduplicate(rows)
    assert [row["id"] for row in kept] == ["a"]
    assert duplicates[0].duplicate_id == "b"


def test_no_known_restrictions_needs_human_review() -> None:
    registry = SourceRegistry(ROOT / "config" / "sources.yaml")
    result = license_precheck(
        {"rights_statement": "No known restrictions on publication."},
        registry.get("library_of_congress_free_to_use"),
    )
    assert result["decision"] == "manual_rights_review_required"


def test_sensitive_history_is_flagged_for_context_review() -> None:
    result = age_precheck(
        {
            "source_id": "library_of_congress_free_to_use",
            "title": "A history of a major battle",
            "description": "Educational historical material.",
        }
    )
    assert result["decision"] == "context_review_required"
