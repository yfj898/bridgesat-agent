"""KnowledgeBackend on PostgreSQL tsvector — golden eval semantics.

Indexes content/packs/bridgesat-math-0.1.0 into the PG tsvector table and
asserts the retrieval pipeline (filters, FTS recall, rerank, citation
validation, explicit no-result) against the golden retrieval set.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import psycopg
import pytest

from app.infrastructure import pg
from app.infrastructure.migration_runner import migrate_database
from app.knowledge.local_backend import KnowledgeBackend, index_pack
from app.knowledge.router import get_backend

ROOT = Path(__file__).resolve().parents[1]
PACK_DIR = ROOT / "content" / "packs" / "bridgesat-math-0.1.0"
DEV = ROOT / "evals" / "retrieval" / "dev.jsonl"
GOLDEN = ROOT / "evals" / "retrieval" / "golden.jsonl"


@pytest.fixture()
def backend() -> KnowledgeBackend:
    admin = pg.connect_admin()
    try:
        migrate_database(admin)
        with admin.transaction():
            index_pack(admin, PACK_DIR)
    finally:
        admin.close()
    conn = pg.connect()
    conn.execute("SELECT set_config('app.tenant_id', 'tenant_demo', false)")
    conn.commit()
    try:
        yield KnowledgeBackend(conn)
    finally:
        conn.rollback()
        conn.close()


@pytest.fixture()
def pack_dir(tmp_path: Path) -> Path:
    destination = tmp_path / PACK_DIR.name
    shutil.copytree(PACK_DIR, destination)
    return destination


def _golden_queries() -> list[dict]:
    return [json.loads(line) for line in GOLDEN.open(encoding="utf-8") if line.strip()]


# --- indexing ------------------------------------------------------------


def test_index_pack_populates_tsvector(backend: KnowledgeBackend) -> None:
    row = backend.connection.execute(
        "SELECT COUNT(*) AS count FROM knowledge_fts"
    ).fetchone()
    assert row["count"] == 71
    generated = backend.connection.execute(
        "SELECT body_tsv IS NOT NULL AS indexed FROM knowledge_fts LIMIT 1"
    ).fetchone()
    assert generated["indexed"] is True
    index = backend.connection.execute(
        "SELECT indexdef FROM pg_indexes "
        "WHERE schemaname = 'public' AND indexname = 'idx_knowledge_fts_tsv'"
    ).fetchone()
    assert "using gin" in index["indexdef"].lower()
    log = backend.connection.execute(
        "SELECT item_count, lesson_count FROM knowledge_index_log "
        "ORDER BY log_id DESC LIMIT 1"
    ).fetchone()
    assert log["item_count"] == 55
    assert log["lesson_count"] == 16


def test_index_pack_is_idempotent(backend: KnowledgeBackend) -> None:
    admin = pg.connect_admin()
    try:
        with admin.transaction():
            index_pack(admin, PACK_DIR)
    finally:
        admin.close()
    row = backend.connection.execute(
        "SELECT COUNT(*) AS count FROM knowledge_fts"
    ).fetchone()
    assert row["count"] == 71


def test_index_pack_rolls_back_failed_rebuild(
    backend: KnowledgeBackend, pack_dir: Path
) -> None:
    lines = (pack_dir / "items.jsonl").read_text(encoding="utf-8").splitlines()
    item = json.loads(lines[0])
    item["version"] = "not-an-integer"
    lines[0] = json.dumps(item, ensure_ascii=False, sort_keys=True)
    (pack_dir / "items.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    admin = pg.connect_admin()
    try:
        with pytest.raises(psycopg.Error):
            index_pack(admin, pack_dir)
    finally:
        admin.close()

    row = backend.connection.execute(
        "SELECT COUNT(*) AS count FROM knowledge_fts"
    ).fetchone()
    assert row["count"] == 71


# --- golden semantics ------------------------------------------------------


def test_lesson_recall_without_exact_prompt_words(backend: KnowledgeBackend) -> None:
    response = backend.retrieve(
        "isolate x when there is a constant on the same side",
        skill="linear_equations",
    )
    ids = [r.content_id for r in response.results]
    assert "math.linear_equations.micro_lesson.001" in ids
    assert "math.linear_equations.micro_lesson.002" in ids


def test_misconception_filtered_golden(backend: KnowledgeBackend) -> None:
    response = backend.retrieve(
        "sign error student kept the sign wrong when isolating",
        skill="linear_equations",
        misconception="sign_error",
    )
    ids = [r.content_id for r in response.results]
    assert "math.linear_equations.001" in ids
    assert "math.linear_equations.002" in ids
    assert "math.linear_equations.003" in ids


def test_explicit_no_result_outside_pack(backend: KnowledgeBackend) -> None:
    response = backend.retrieve("unknown algebra topic completely outside the pack")
    assert response.results == []
    assert response.explicit_no_result is True


def test_all_golden_expected_ids_recalled(backend: KnowledgeBackend) -> None:
    for entry in _golden_queries():
        response = backend.retrieve(
            entry["query"],
            skill=entry.get("skill"),
            misconception=entry.get("misconception"),
            max_results=3,
        )
        ids = [r.content_id for r in response.results]
        expected = set(entry.get("expected_ids") or [])
        assert expected <= set(ids), f"query={entry['query']!r} missing {expected - set(ids)}"


def test_dev_expected_ids_recalled_at_eval_cutoff(backend: KnowledgeBackend) -> None:
    for line in DEV.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        response = backend.retrieve(
            entry["query"], skill=entry.get("skill"), max_results=3
        )
        ids = {result.content_id for result in response.results}
        expected = set(entry.get("expected_ids") or [])
        assert expected <= ids, f"query={entry['query']!r} missing {expected - ids}"


# --- citation / metadata integrity ----------------------------------------


def test_results_carry_citations_and_licenses(backend: KnowledgeBackend) -> None:
    response = backend.retrieve("solve linear equation 8x - 1 = 87", skill="linear_equations")
    assert response.results
    for result in response.results:
        assert result.review_status == "published"
        assert result.citation.startswith("BridgeSAT math item")
        assert result.license_id == "bridgesat_original"
        assert result.source_id == "deepmind_mathematics_dataset"


def test_audience_and_license_filters(backend: KnowledgeBackend) -> None:
    admin = pg.connect_admin()
    try:
        with admin.transaction():
            admin.execute(
                "UPDATE knowledge_fts SET audience = 'teacher' "
                "WHERE content_id = 'math.linear_equations.001'"
            )
        response = backend.retrieve("linear equation", skill="linear_equations")
        assert all(r.audience == "student" for r in response.results)

        with admin.transaction():
            admin.execute(
                "UPDATE knowledge_fts SET license_id = 'cc-by-nc-4.0' "
                "WHERE content_id = 'math.linear_equations.001'"
            )
        response = backend.retrieve(
            "linear equation",
            skill="linear_equations",
            allowed_licenses=("bridgesat_original",),
        )
        assert all(r.license_id == "bridgesat_original" for r in response.results)
    finally:
        admin.close()


def test_deterministic_reranking(backend: KnowledgeBackend) -> None:
    first = backend.retrieve("linear equation", skill="linear_equations", max_results=5)
    second = backend.retrieve("linear equation", skill="linear_equations", max_results=5)
    assert [r.content_id for r in first.results] == [r.content_id for r in second.results]


def test_recently_shown_downweights(backend: KnowledgeBackend) -> None:
    baseline = [r.content_id for r in backend.retrieve(
        "linear equation", skill="linear_equations", max_results=20
    ).results]
    response = backend.retrieve(
        "linear equation",
        skill="linear_equations",
        recently_shown={"math.linear_equations.001"},
        max_results=20,
    )
    ids = [r.content_id for r in response.results]
    assert ids.index("math.linear_equations.001") > baseline.index("math.linear_equations.001")


def test_empty_license_filter_is_explicit_no_result(backend: KnowledgeBackend) -> None:
    response = backend.retrieve("linear equation", allowed_licenses=())
    assert response.results == []
    assert response.explicit_no_result is True


def test_router_reuses_the_request_connection() -> None:
    class Connection:
        pass

    class Request:
        class State:
            pass

        state = State()

    connection = Connection()
    request = Request()
    request.state.connection = connection

    backend = get_backend(request)

    assert backend.connection is connection
