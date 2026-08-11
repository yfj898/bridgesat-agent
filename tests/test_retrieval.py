"""Governed retrieval tests: tsvector indexing, filters, prerequisite
expansion, citation/license validation, restricted-source exclusion."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.content_pipeline.contracts import SCHEMA_VERSION, content_hash
from app.content_pipeline.packaging import PACK_VERSION, build_pack
from app.infrastructure import pg
from app.infrastructure.migration_runner import migrate_database
from app.knowledge.local_backend import (
    KnowledgeBackend,
    RestrictedSourceError,
    UnpublishedPackError,
    index_pack,
)
from app.knowledge.hierarchy import prerequisites_of

PUBLISHED = "published"


def _item(
    content_id: str,
    skill: str,
    subskill: str = "isolate_variables",
    *,
    prompt: str | None = None,
    review_status: str = "approved",
    audience: str = "student",
    license_id: str = "bridgesat_original",
    license_name: str = "BridgeSAT original content",
    source_id: str = "deepmind_mathematics_dataset",
    difficulty: int = 2,
    misconception: str = "sign_error",
) -> dict:
    item = {
        "id": content_id,
        "version": 1,
        "schema_version": SCHEMA_VERSION,
        "domain": "math",
        "content_type": "question",
        "target_skill": skill,
        "target_subskill": subskill,
        "required_prerequisites": ["integer_operations"],
        "difficulty": difficulty,
        "prompt": prompt or f"If 2x + 3 = 7, what is the value of x? ({content_id})",
        "choices": [
            {"id": "A", "text": "2"},
            {"id": "B", "text": "-2"},
            {"id": "C", "text": "8"},
            {"id": "D", "text": "3"},
        ],
        "answer_choice_id": "A",
        "misconception_map": {
            "B": misconception,
            "C": "inverse_operation_error",
            "D": "arithmetic_error",
        },
        "hints": [
            {"level": 1, "text": "Subtract 3."},
            {"level": 2, "text": "Divide by 2."},
            {"level": 3, "text": "x = 2."},
        ],
        "worked_explanation": "2x = 4, x = 2.",
        "estimated_seconds": 60,
        "source_lineage": {"source_id": source_id, "lineage_id": "x", "role": "concept_source_only"},
        "license": {"id": license_id, "name": license_name},
        "review_status": review_status,
        "reviewers": {r: r for r in ("educational", "answer", "license", "accessibility")},
        "release_batch": "b1",
        "content_hash": "",
        "author_metadata": {"kind": "expression", "expression": "2*2 + 3", "expected": "7"},
    }
    item["content_hash"] = content_hash(item)
    return item


def _lesson(content_id: str, skill: str, *, title: str, body: str) -> dict:
    lesson = {
        "id": content_id,
        "version": 1,
        "schema_version": SCHEMA_VERSION,
        "domain": "math",
        "content_type": "lesson",
        "target_skill": skill,
        "target_subskill": "",
        "required_prerequisites": ["integer_operations"],
        "difficulty": 1,
        "title": title,
        "body": body,
        "estimated_seconds": 120,
        "source_lineage": {"source_id": "deepmind_mathematics_dataset", "lineage_id": "x", "role": "concept_source_only"},
        "license": {"id": "bridgesat_original", "name": "BridgeSAT original content"},
        "review_status": "approved",
        "reviewers": {r: r for r in ("educational", "answer", "license", "accessibility")},
        "release_batch": "b1",
        "content_hash": "",
    }
    lesson["content_hash"] = content_hash(lesson)
    return lesson


@pytest.fixture()
def pack_dir(tmp_path: Path) -> Path:
    items = [
        _item("math.linear_equations.001", "linear_equations", prompt="If 8x - 1 = 87, what is the value of x?"),
        _item("math.linear_equations.002", "linear_equations", prompt="If 5x + 2 = -58, what is the value of x?", difficulty=1),
        _item("math.systems_equations.001", "systems_equations", subskill="solve_systems", prompt="What is the solution (x, y) to the system 4x + y = 30 and 4x + 3y = 42?", difficulty=3),
        _item("math.functions_models.010", "functions_models", subskill="function_evaluation", prompt="If f(x) = 7x - 6, what is the value of f(-4)?", difficulty=1),
    ]
    lessons = [
        _lesson(
            "math.linear_equations.micro_lesson.001",
            "linear_equations",
            title="Solving Linear Equations",
            body="To solve ax + b = c, isolate the variable term by subtracting b from both sides, then divide both sides by a.",
        ),
        _lesson(
            "math.systems_equations.micro_lesson.001",
            "systems_equations",
            title="Solving Systems of Equations",
            body="A system of two linear equations can be solved by elimination: multiply one equation so a variable matches, subtract, solve, substitute back.",
        ),
    ]
    root = tmp_path / "packs"
    build_pack(items, lessons, out_dir=root)
    return root / f"bridgesat-math-{PACK_VERSION}"


@pytest.fixture()
def backend(pack_dir: Path, pg_tenant: str) -> KnowledgeBackend:
    admin = pg.connect_admin()
    try:
        migrate_database(admin)
        with admin.transaction():
            index_pack(admin, pack_dir)
    finally:
        admin.close()
    conn = pg.connect()
    conn.execute("SELECT set_config('app.tenant_id', %s, false)", ("tenant_test",))
    conn.commit()
    try:
        yield KnowledgeBackend(conn)
    finally:
        conn.rollback()
        conn.close()


# --- indexing ------------------------------------------------------------


def test_index_pack_counts(backend: KnowledgeBackend) -> None:
    row = backend.connection.execute("SELECT COUNT(*) AS count FROM knowledge_fts").fetchone()
    log = backend.connection.execute(
        "SELECT item_count, lesson_count FROM knowledge_index_log "
        "ORDER BY log_id DESC LIMIT 1"
    ).fetchone()
    assert row["count"] == 6
    assert log["item_count"] == 4
    assert log["lesson_count"] == 2


def test_index_pack_rejects_unpublished(backend: KnowledgeBackend, tmp_path: Path) -> None:
    item = _item("math.linear_equations.001", "linear_equations")
    root = tmp_path / "unpublished-packs"
    build_pack([item], [], out_dir=root)
    built = root / f"bridgesat-math-{PACK_VERSION}"
    manifest = json.loads((built / "manifest.json").read_text(encoding="utf-8"))
    manifest["status"] = "draft"
    (built / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )
    admin = pg.connect_admin()
    try:
        with pytest.raises(UnpublishedPackError):
            index_pack(admin, built)
    finally:
        admin.close()


def test_restricted_source_never_indexed(pack_dir: Path, backend: KnowledgeBackend) -> None:
    # Simulate a pack that contains an item drawn from a restricted source.
    items_json = json.loads((pack_dir / "items.jsonl").read_text(encoding="utf-8").splitlines()[0])
    items_json["source_lineage"] = {
        "source_id": "gsm8k",
        "lineage_id": "g-1",
        "role": "question_candidate",
    }
    (pack_dir / "items.jsonl").write_text(
        json.dumps(items_json, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )
    admin = pg.connect_admin()
    try:
        with pytest.raises(RestrictedSourceError):
            index_pack(admin, pack_dir)
    finally:
        admin.close()


# --- retrieval pipeline --------------------------------------------------


def test_retrieve_returns_published_items_with_citations(backend: KnowledgeBackend) -> None:
    response = backend.retrieve("solve linear equation 8x - 1 = 87", skill="linear_equations")
    assert response.results
    first = response.results[0]
    assert first.review_status == "published"
    assert first.citation.startswith("BridgeSAT math item")
    assert first.license_id == "bridgesat_original"
    assert first.source_id == "deepmind_mathematics_dataset"


def test_retrieve_explicit_no_result(backend: KnowledgeBackend) -> None:
    response = backend.retrieve("quantum chromodynamics string theory")
    assert response.results == []
    assert response.explicit_no_result is True


def test_bare_query_with_single_content_term_is_no_result(backend: KnowledgeBackend) -> None:
    # "unknown" alone matches a few bodies ("solve for the unknown"), so a
    # bare out-of-domain query must not ride a single shared word into
    # results. Stopwords and one content term are not enough evidence.
    response = backend.retrieve("unknown algebra topic completely outside the pack")
    assert response.results == []
    assert response.explicit_no_result is True


def test_bare_query_with_two_content_terms_returns_results(backend: KnowledgeBackend) -> None:
    response = backend.retrieve("linear equations solve")
    assert response.results
    assert response.explicit_no_result is False


def test_audience_filter_excludes_other_audience(backend: KnowledgeBackend) -> None:
    admin = pg.connect_admin()
    try:
        with admin.transaction():
            admin.execute(
                "UPDATE knowledge_fts SET audience = 'teacher' "
                "WHERE content_id = 'math.linear_equations.001'"
            )
    finally:
        admin.close()
    response = backend.retrieve("linear equation 8x - 1 = 87", skill="linear_equations")
    assert all(result.audience == "student" for result in response.results)


def test_license_filter_excludes_disallowed_license(backend: KnowledgeBackend) -> None:
    admin = pg.connect_admin()
    try:
        with admin.transaction():
            admin.execute(
                "UPDATE knowledge_fts SET license_id = 'cc-by-nc-4.0', license_name = 'CC BY-NC' "
                "WHERE content_id = 'math.linear_equations.001'"
            )
    finally:
        admin.close()
    response = backend.retrieve(
        "linear equation", skill="linear_equations", allowed_licenses=("bridgesat_original",)
    )
    assert all(result.license_id == "bridgesat_original" for result in response.results)


def test_skill_and_subskill_filters(backend: KnowledgeBackend) -> None:
    response = backend.retrieve(
        "function evaluation", skill="functions_models", subskill="function_evaluation"
    )
    assert response.results
    assert all(result.target_skill == "functions_models" for result in response.results)
    assert all(result.target_subskill == "function_evaluation" for result in response.results)


def test_misconception_filter(backend: KnowledgeBackend) -> None:
    response = backend.retrieve("sign error", skill="linear_equations", misconception="sign_error")
    assert response.results
    assert all("sign_error" in result.body for result in response.results)


def test_prerequisite_expansion_max_two_hops() -> None:
    assert prerequisites_of("systems_equations", max_hops=2) == [
        "systems_equations",
        "integer_operations",
        "linear_equations",
    ]
    assert prerequisites_of("linear_equations", max_hops=2) == [
        "linear_equations",
        "integer_operations",
    ]


def test_expansion_includes_prerequisite_lessons(backend: KnowledgeBackend) -> None:
    response = backend.retrieve("solve a system of equations", skill="systems_equations")
    skills = {result.target_skill for result in response.results}
    assert "systems_equations" in skills
    assert response.expanded_skills == [
        "systems_equations",
        "integer_operations",
        "linear_equations",
    ]


def test_deterministic_reranking(backend: KnowledgeBackend) -> None:
    first = backend.retrieve("linear equation", skill="linear_equations", max_results=5)
    second = backend.retrieve("linear equation", skill="linear_equations", max_results=5)
    assert [r.content_id for r in first.results] == [r.content_id for r in second.results]


def test_recently_shown_downweights(backend: KnowledgeBackend) -> None:
    response = backend.retrieve(
        "linear equation", skill="linear_equations", recently_shown={"math.linear_equations.001"},
        max_results=20,
    )
    ids = [r.content_id for r in response.results]
    baseline = [r.content_id for r in backend.retrieve(
        "linear equation", skill="linear_equations", max_results=20
    ).results]
    assert ids.index("math.linear_equations.001") > baseline.index("math.linear_equations.001")


def test_every_result_has_complete_metadata(backend: KnowledgeBackend) -> None:
    response = backend.retrieve("linear equation", skill="linear_equations", max_results=10)
    for result in response.results:
        assert result.content_id and result.version
        assert result.source_id and result.license_id and result.license_name
        assert result.review_status and result.citation
        assert result.body
