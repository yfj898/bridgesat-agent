from __future__ import annotations

import json
import threading
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.knowledge.router as knowledge_router_module
import app.main as main_module
import app.sync.router as sync_router_module
from app.auth import TokenStore
from app.infrastructure import pg
from app.infrastructure.pg import transaction
from app.knowledge import router as knowledge_router
from app.knowledge.local_backend import RetrievalResponse
from app.main import create_app
from app.memory.deletion import StudentMemoryDeletionService
from app.memory.outbox import student_advisory_lock
from app.models import Skill, StudentCreate
from app.repository import StudentRepository
from tests.pg_test_helpers import import_fixture_pack


class _FakeCursor:
    def __init__(self, row: dict[str, Any] | None = None) -> None:
        self.row = row

    def fetchone(self) -> dict[str, Any] | None:
        return self.row


class _FakeConnection:
    def __init__(self, *, rollback_error: BaseException | None = None) -> None:
        self.closed = False
        self.rollback_calls = 0
        self.commit_calls = 0
        self.tenant_id: str | None = None
        self.student_insert_tenant: str | None = None
        self.token_insert_tenant: str | None = None
        self.rollback_error = rollback_error

    def execute(self, query: str, params: tuple[Any, ...] | None = None) -> _FakeCursor:
        normalized = " ".join(str(query).split()).lower()
        if "set_config('app.tenant_id'" in normalized:
            assert params is not None
            self.tenant_id = str(params[0])
        elif "insert into students" in normalized:
            self.student_insert_tenant = self.tenant_id
        elif "insert into student_tokens" in normalized:
            self.token_insert_tenant = self.tenant_id
        return _FakeCursor()

    def commit(self) -> None:
        self.commit_calls += 1

    def rollback(self) -> None:
        self.rollback_calls += 1
        if self.rollback_error is not None:
            raise self.rollback_error

    def close(self) -> None:
        self.closed = True


class _ScopedCursor:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or []
        self.rowcount = len(self.rows)

    def fetchone(self) -> dict[str, Any] | None:
        return self.rows[0] if self.rows else None

    def fetchall(self) -> list[dict[str, Any]]:
        return list(self.rows)


class _ScopedDatabase:
    def __init__(self) -> None:
        self.students: dict[tuple[str, str], dict[str, Any]] = {}
        self.tokens: dict[str, dict[str, str]] = {}

    def seed_student(self, tenant_id: str, name: str) -> tuple[str, str]:
        connection = _ScopedConnection(self)
        connection.tenant_id = tenant_id
        student = StudentRepository(connection).create(
            StudentCreate(name=name, daily_minutes=15, target_score=1100)
        )
        token = TokenStore(connection).issue(student.id)
        connection.close()
        return student.id, token


class _ScopedConnection:
    def __init__(self, database: _ScopedDatabase) -> None:
        self.database = database
        self.closed = False
        self.rollback_calls = 0
        self.commit_calls = 0
        self.tenant_id: str | None = None

    def execute(
        self, query: str, params: tuple[Any, ...] | list[Any] | None = None
    ) -> _ScopedCursor:
        normalized = " ".join(str(query).split()).lower()
        values = tuple(params or ())
        if "set_config('app.tenant_id'" in normalized:
            self.tenant_id = str(values[0])
            return _ScopedCursor()

        if "pg_advisory_unlock" in normalized:
            return _ScopedCursor([{"unlocked": True}])
        if "pg_advisory_lock" in normalized:
            return _ScopedCursor()

        if "from resolve_token" in normalized:
            token_hash = str(values[0])
            token = self.database.tokens.get(token_hash)
            if token is None:
                return _ScopedCursor()
            if "tenant_id, student_id" in normalized:
                return _ScopedCursor([token])
            return _ScopedCursor([{"student_id": token["student_id"]}])

        if "insert into students" in normalized:
            assert self.tenant_id is not None
            student_id, name, daily_minutes, target_score, mastery_json = values[:5]
            self.database.students[(self.tenant_id, str(student_id))] = {
                "id": str(student_id),
                "name": str(name),
                "daily_minutes": int(daily_minutes),
                "target_score": int(target_score),
                "mastery_json": str(mastery_json),
            }
            return _ScopedCursor()

        if "insert into student_tokens" in normalized:
            assert self.tenant_id is not None
            _, student_id, token_hash = values[:3]
            self.database.tokens[str(token_hash)] = {
                "tenant_id": self.tenant_id,
                "student_id": str(student_id),
            }
            return _ScopedCursor()

        if normalized.startswith("select * from students"):
            assert self.tenant_id is not None
            student_id = str(values[0])
            student = self.database.students.get((self.tenant_id, student_id))
            return _ScopedCursor([student] if student is not None else [])

        if normalized.startswith("update students set mastery_json"):
            assert self.tenant_id is not None
            mastery_json, student_id = values[:2]
            student = self.database.students.get((self.tenant_id, str(student_id)))
            if student is not None:
                student["mastery_json"] = str(mastery_json)
                return _ScopedCursor([student])
            return _ScopedCursor()

        return _ScopedCursor()

    def commit(self) -> None:
        self.commit_calls += 1

    def rollback(self) -> None:
        self.rollback_calls += 1

    def close(self) -> None:
        self.closed = True


def test_app_factory_accepts_an_injected_connection_factory() -> None:
    connection = _FakeConnection()
    calls = 0

    def connection_factory() -> _FakeConnection:
        nonlocal calls
        calls += 1
        return connection

    app = create_app(connection_factory, run_migrations=False)

    assert app.title == "BridgeSAT Agent"
    assert calls == 0


def test_public_student_creation_uses_the_default_tenant() -> None:
    connection = _FakeConnection()
    app = create_app(lambda: connection, run_migrations=False)

    with TestClient(app) as client:
        response = client.post(
            "/v1/students",
            json={"name": "Ari", "daily_minutes": 15, "target_score": 1100},
        )

    assert response.status_code == 201
    assert connection.tenant_id == "tenant_demo"
    assert connection.student_insert_tenant == "tenant_demo"
    assert connection.token_insert_tenant == "tenant_demo"
    assert connection.rollback_calls == 1
    assert connection.closed is True


def test_database_requests_get_distinct_connections_and_cleanup() -> None:
    connections: list[_FakeConnection] = []

    def connection_factory() -> _FakeConnection:
        connection = _FakeConnection()
        connections.append(connection)
        return connection

    app = create_app(connection_factory, run_migrations=False)

    with TestClient(app) as client:
        first = client.post(
            "/v1/students",
            json={"name": "Ari", "daily_minutes": 15, "target_score": 1100},
        )
        second = client.post(
            "/v1/students",
            json={"name": "Bea", "daily_minutes": 20, "target_score": 1200},
        )

    assert first.status_code == 201
    assert second.status_code == 201
    assert len(connections) == 2
    assert connections[0] is not connections[1]
    for connection in connections:
        assert connection.rollback_calls == 1
        assert connection.closed is True


def test_lifespan_probes_configured_app_before_migration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[object] = []

    class _LifecycleConnection:
        def __init__(self, name: str) -> None:
            self.name = name
            self.rollback_calls = 0
            self.close_calls = 0

        def rollback(self) -> None:
            self.rollback_calls += 1

        def close(self) -> None:
            self.close_calls += 1

    admin = _LifecycleConnection("admin")
    probe = _LifecycleConnection("probe")
    monkeypatch.setattr(main_module.pg, "connect_admin", lambda: admin)
    monkeypatch.setattr(
        main_module.pg,
        "assert_matching_database",
        lambda current_admin, current_probe: events.append(
            ("pair", current_admin.name, current_probe.name)
        ),
    )
    monkeypatch.setattr(
        main_module, "migrate_database", lambda current_admin: events.append("migrate")
    )

    application = create_app(lambda: probe, run_migrations=True)
    with TestClient(application):
        pass

    assert events == [("pair", "admin", "probe"), "migrate"]
    assert probe.rollback_calls == 1
    assert probe.close_calls == 1
    assert admin.rollback_calls == 1
    assert admin.close_calls == 1


@pytest.mark.parametrize(
    "path",
    [
        "/health",
        "/v1/questions",
        "/",
        "/sw.js",
    ],
)
def test_database_independent_paths_bypass_connection_factory(path: str) -> None:
    calls = 0

    def connection_factory() -> _FakeConnection:
        nonlocal calls
        calls += 1
        raise AssertionError("database-independent path opened a connection")

    app = create_app(connection_factory, run_migrations=False)

    with TestClient(app) as client:
        response = client.get(path)

    assert response.status_code == 200
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert calls == 0


def _register_pack_row(
    dsn: str,
    *,
    pack_id: str,
    pack_version: str,
    manifest: dict | None = None,
    include_items: bool = True,
) -> None:
    """Insert one content_packs row (and required content rows) directly so the
    negative registry paths (404/409/503) are exercised without relying on the
    importing pipeline."""
    admin = pg.connect_admin(dsn)
    try:
        manifest_json = manifest or {
            "pack_id": pack_id,
            "pack_version": pack_version,
            "status": "published",
        }
        with transaction(admin):
            admin.execute(
                """
                INSERT INTO content_packs (pack_id, pack_version, status,
                    manifest_json, created_at)
                VALUES (%s, %s, 'published', %s, '2026-01-01T00:00:00')
                """,
                (pack_id, pack_version, json.dumps(manifest_json)),
            )
            if include_items:
                admin.execute(
                    """
                    INSERT INTO content_items (content_id, version, content_type,
                        review_status, status, target_skill)
                    VALUES (%s, 1, 'question', 'approved', 'approved',
                        'linear_equations')
                    """,
                    (f"{pack_id}.q1",),
                )
                admin.execute(
                    """
                    INSERT INTO content_item_versions (content_id, version,
                        item_json, content_hash, versioned_body, versioned_at)
                    VALUES (%s, 1, %s, 'sha256:baditem',
                        '{"id": "not json', '2026-01-01T00:00:00')
                    """,
                    (f"{pack_id}.q1", '{"id": "not json'),
                )
                admin.execute(
                    """
                    INSERT INTO content_pack_items (pack_id, content_id, version)
                    VALUES (%s, %s, 1)
                    """,
                    (pack_id, f"{pack_id}.q1"),
                )
    finally:
        pg.quiet_close(admin)


def test_content_pack_endpoint_reads_the_postgres_registry(
    isolated_pg_database, monkeypatch, tmp_path
) -> None:
    import_fixture_pack(isolated_pg_database.admin_dsn)
    fake_pack = tmp_path / "syncmath-0.1.0"
    fake_pack.mkdir()
    (fake_pack / "manifest.json").write_text(
        json.dumps(
            {
                "pack_id": "syncmath",
                "pack_version": "0.1.0",
                "status": "published",
            }
        ),
        encoding="utf-8",
    )
    (fake_pack / "items.jsonl").write_text(
        json.dumps({"id": "filesystem-item", "content_type": "question"}) + "\n",
        encoding="utf-8",
    )
    (fake_pack / "lessons.jsonl").write_text(
        json.dumps(
            {
                "id": "filesystem-lesson",
                "content_type": "worked_example",
                "review_status": "approved",
            }
        ) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("BRIDGESAT_PACKS_ROOT", str(tmp_path))
    application = create_app(
        isolated_pg_database.connect_app,
        run_migrations=False,
    )

    with TestClient(application) as client:
        response = client.get("/v1/content-packs/0.1.0")

    assert response.status_code == 200
    body = response.json()
    assert body["manifest"]["pack_id"] == "syncmath"
    assert {item["id"] for item in body["items"]} == {
        "sync.linear.001",
        "sync.ratios.001",
    }
    assert {lesson["id"] for lesson in body["lessons"]} == {
        "we_linear_001",
        "ml_linear_001",
    }


def test_content_pack_endpoint_returns_404_for_unknown_version(
    isolated_pg_database,
) -> None:
    import_fixture_pack(isolated_pg_database.admin_dsn)
    application = create_app(
        isolated_pg_database.connect_app,
        run_migrations=False,
    )

    with TestClient(application) as client:
        response = client.get("/v1/content-packs/9.9.9")

    assert response.status_code == 404
    assert "9.9.9" in response.json()["detail"]


def test_content_pack_endpoint_returns_409_for_ambiguous_version(
    isolated_pg_database,
) -> None:
    _register_pack_row(
        isolated_pg_database.admin_dsn,
        pack_id="clash-a",
        pack_version="5.0.0",
    )
    _register_pack_row(
        isolated_pg_database.admin_dsn,
        pack_id="clash-b",
        pack_version="5.0.0",
    )
    application = create_app(
        isolated_pg_database.connect_app,
        run_migrations=False,
    )

    with TestClient(application) as client:
        response = client.get("/v1/content-packs/5.0.0")

    assert response.status_code == 409
    assert "ambiguous" in response.json()["detail"]


@pytest.mark.parametrize(
    ("manifest", "detail"),
    [
        (
            {"pack_id": "clash-other", "pack_version": "0.2.0", "status": "published"},
            "manifest mismatch",
        ),
        (
            {"pack_id": "clash-c", "pack_version": "0.3.0", "status": "withdrawn"},
            "manifest mismatch",
        ),
    ],
)
def test_content_pack_endpoint_returns_503_on_manifest_mismatch(
    isolated_pg_database, manifest, detail
) -> None:
    _register_pack_row(
        isolated_pg_database.admin_dsn,
        pack_id="clash-c",
        pack_version="0.2.0",
        manifest=manifest,
    )
    application = create_app(
        isolated_pg_database.connect_app,
        run_migrations=False,
    )

    with TestClient(application) as client:
        response = client.get("/v1/content-packs/0.2.0")

    assert response.status_code == 503
    assert detail in response.json()["detail"]


def test_content_pack_endpoint_returns_503_on_invalid_item_json(
    isolated_pg_database,
) -> None:
    _register_pack_row(
        isolated_pg_database.admin_dsn,
        pack_id="clash-d",
        pack_version="0.2.0",
    )
    application = create_app(
        isolated_pg_database.connect_app,
        run_migrations=False,
    )

    with TestClient(application) as client:
        response = client.get("/v1/content-packs/0.2.0")

    assert response.status_code == 503
    assert "Invalid content registry item" in response.json()["detail"]


def test_request_cleanup_closes_connection_when_rollback_raises() -> None:
    connection = _FakeConnection(rollback_error=RuntimeError("rollback failed"))
    app = create_app(lambda: connection, run_migrations=False)

    with pytest.raises(RuntimeError, match="rollback failed"):
        with TestClient(app) as client:
            client.post(
                "/v1/students",
                json={"name": "Ari", "daily_minutes": 15, "target_score": 1100},
            )

    assert connection.rollback_calls == 1
    assert connection.closed is True


@pytest.mark.parametrize(
    ("headers", "detail"),
    [
        ({}, "Bearer token required"),
        ({"Authorization": "Bearer invalid-token"}, "Invalid or revoked token"),
    ],
)
def test_missing_or_invalid_tokens_return_401(
    headers: dict[str, str], detail: str
) -> None:
    database = _ScopedDatabase()
    app = create_app(lambda: _ScopedConnection(database), run_migrations=False)

    with TestClient(app) as client:
        response = client.post(
            "/v1/diagnostics",
            headers=headers,
            json={
                "answers": [
                    {
                        "question_id": "linear-001",
                        "selected_answer": "3",
                        "hint_level": 0,
                    }
                ]
            },
        )

    assert response.status_code == 401
    assert response.json()["detail"] == detail


def test_authenticated_diagnostic_and_adapt_use_request_scoped_store() -> None:
    database = _ScopedDatabase()
    student_id, token = database.seed_student("tenant_demo", "Ari")
    app = create_app(lambda: _ScopedConnection(database), run_migrations=False)
    headers = {"Authorization": f"Bearer {token}"}

    with TestClient(app) as client:
        diagnostic = client.post(
            "/v1/diagnostics",
            headers=headers,
            json={
                "answers": [
                    {
                        "question_id": "linear-001",
                        "selected_answer": "3",
                        "hint_level": 0,
                    }
                ]
            },
        )
        adapted = client.post(
            "/v1/adapt",
            headers=headers,
            json={
                "skill": "linear_equations",
                "was_correct": True,
                "hint_level": 0,
                "consecutive_skill_errors": 0,
                "minutes_remaining": 20,
            },
        )

    assert diagnostic.status_code == 200
    assert diagnostic.json()["student_id"] == student_id
    assert adapted.status_code == 200
    assert adapted.json()["action"] == "continue_practice"


def test_deletion_pending_token_cannot_write_diagnostic_or_adapt(
    client: TestClient,
    pg_connection,
) -> None:
    student = StudentRepository(pg_connection).create(
        StudentCreate(name="Deletion Pending", daily_minutes=15, target_score=1100)
    )
    token = TokenStore(pg_connection).issue(student.id)
    StudentMemoryDeletionService(pg_connection).request_deletion(student.id)

    before = pg_connection.execute(
        "SELECT mastery_json FROM students WHERE id = %s", (student.id,)
    ).fetchone()["mastery_json"]
    headers = {"Authorization": f"Bearer {token}"}

    diagnostic = client.post(
        "/v1/diagnostics",
        headers=headers,
        json={
            "answers": [
                {
                    "question_id": "linear-001",
                    "selected_answer": "3",
                    "hint_level": 0,
                }
            ]
        },
    )
    adapted = client.post(
        "/v1/adapt",
        headers=headers,
        json={
            "skill": "linear_equations",
            "was_correct": True,
            "hint_level": 0,
            "consecutive_skill_errors": 0,
            "minutes_remaining": 20,
        },
    )

    assert diagnostic.status_code in {401, 409}
    assert adapted.status_code in {401, 409}
    assert pg_connection.execute(
        "SELECT mastery_json FROM students WHERE id = %s", (student.id,)
    ).fetchone()["mastery_json"] == before


def test_update_mastery_rejects_non_active_tenant_student(pg_connection) -> None:
    student = StudentRepository(pg_connection).create(
        StudentCreate(name="Inactive", daily_minutes=15, target_score=1100)
    )
    pg_connection.execute(
        "UPDATE students SET status = 'deletion_pending' WHERE id = %s",
        (student.id,),
    )
    pg_connection.commit()

    with pytest.raises(ValueError, match="active"):
        StudentRepository(pg_connection).update_mastery(student.id, student.mastery)


def test_update_mastery_can_join_caller_owned_transaction(
    pg_connection,
    isolated_pg_database,
    pg_tenant,
) -> None:
    other = isolated_pg_database.connect_app()
    other.execute("SELECT set_config('app.tenant_id', %s, false)", (pg_tenant,))
    other.commit()
    try:
        student = StudentRepository(pg_connection).create(
            StudentCreate(name="Transaction Owner", daily_minutes=15, target_score=1100)
        )
        mastery = dict(student.mastery)
        mastery[Skill.LINEAR_EQUATIONS] = 0.9

        with transaction(pg_connection):
            StudentRepository(pg_connection).update_mastery(
                student.id, mastery, commit=False
            )
            row = other.execute(
                "SELECT mastery_json FROM students WHERE id = %s", (student.id,)
            ).fetchone()
            assert json.loads(row["mastery_json"])["linear_equations"] == 0.5

        row = other.execute(
            "SELECT mastery_json FROM students WHERE id = %s", (student.id,)
        ).fetchone()
        assert json.loads(row["mastery_json"])["linear_equations"] == 0.9
    finally:
        pg.quiet_close(other)


class _ReadSignalingConnection:
    def __init__(self, connection, read_started: threading.Event) -> None:
        self.connection = connection
        self.read_started = read_started

    def execute(self, query, params=None, **kwargs):  # noqa: ANN001
        result = self.connection.execute(query, params, **kwargs)
        normalized = " ".join(str(query).split()).lower()
        if normalized.startswith("select * from students"):
            self.read_started.set()
        return result

    def __getattr__(self, name: str):
        return getattr(self.connection, name)


def test_adapt_serializes_read_compute_write_across_postgres_connections(
    isolated_pg_database,
    pg_tenant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed = isolated_pg_database.connect_app()
    seed.execute("SELECT set_config('app.tenant_id', %s, false)", (pg_tenant,))
    seed.commit()
    student = StudentRepository(seed).create(
        StudentCreate(name="Concurrent Mastery", daily_minutes=15, target_score=1100)
    )
    token = TokenStore(seed).issue(student.id)
    read_started = threading.Event()

    def connection_factory():
        return _ReadSignalingConnection(
            isolated_pg_database.connect_app(), read_started
        )

    app = create_app(connection_factory, run_migrations=False)
    monkeypatch.setattr(main_module, "_llm_client", None)
    responses: list[object] = []

    def call_adapt(client: TestClient) -> None:
        responses.append(
            client.post(
                "/v1/adapt",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "skill": "linear_equations",
                    "was_correct": True,
                    "hint_level": 0,
                    "consecutive_skill_errors": 0,
                    "minutes_remaining": 20,
                },
            )
        )

    try:
        with TestClient(app) as client:
            with student_advisory_lock(seed, student.id):
                seed.execute(
                    """
                    SELECT status
                    FROM students
                    WHERE id = %s
                      AND tenant_id = current_setting('app.tenant_id', true)
                    FOR UPDATE
                    """,
                    (student.id,),
                ).fetchone()
                thread = threading.Thread(target=call_adapt, args=(client,), daemon=True)
                thread.start()
                read_was_reached = read_started.wait(timeout=0.25)

                mastery = json.loads(
                    seed.execute(
                        "SELECT mastery_json FROM students WHERE id = %s",
                        (student.id,),
                    ).fetchone()["mastery_json"]
                )
                mastery["linear_equations"] = 0.6
                seed.execute(
                    """
                    UPDATE students
                    SET mastery_json = %s
                    WHERE id = %s
                      AND tenant_id = current_setting('app.tenant_id', true)
                    """,
                    (json.dumps(mastery), student.id),
                )
                seed.commit()

        thread.join(timeout=5)
        assert not thread.is_alive()
        assert not read_was_reached
        assert len(responses) == 1
        assert responses[0].status_code == 200
        row = seed.execute(
            "SELECT mastery_json FROM students WHERE id = %s", (student.id,)
        ).fetchone()
        assert json.loads(row["mastery_json"])["linear_equations"] == pytest.approx(0.67)
    finally:
        pg.quiet_close(seed)


def test_snapshot_waits_for_student_advisory_lock(
    isolated_pg_database,
    pg_tenant,
) -> None:
    seed = isolated_pg_database.connect_app()
    seed.execute("SELECT set_config('app.tenant_id', %s, false)", (pg_tenant,))
    seed.commit()
    student = StudentRepository(seed).create(
        StudentCreate(name="Snapshot Lock", daily_minutes=15, target_score=1100)
    )
    token = TokenStore(seed).issue(student.id)
    read_started = threading.Event()
    request_started = threading.Event()

    def connection_factory():
        return _ReadSignalingConnection(
            isolated_pg_database.connect_app(), read_started
        )

    app = create_app(connection_factory, run_migrations=False)
    responses: list[object] = []

    def call_snapshot(client: TestClient) -> None:
        request_started.set()
        responses.append(
            client.get(
                "/v1/sync/snapshot",
                headers={"Authorization": f"Bearer {token}"},
            )
        )

    try:
        with TestClient(app) as client:
            with student_advisory_lock(seed, student.id):
                thread = threading.Thread(
                    target=call_snapshot, args=(client,), daemon=True
                )
                thread.start()
                assert request_started.wait(timeout=2)
                read_was_reached = read_started.wait(timeout=0.25)

            thread.join(timeout=5)
            assert not thread.is_alive()

        assert not read_was_reached
        assert len(responses) == 1
        assert responses[0].status_code == 200
    finally:
        pg.quiet_close(seed)


def test_two_tenants_cannot_read_each_others_student() -> None:
    database = _ScopedDatabase()
    student_a, token_a = database.seed_student("tenant_a", "Ari")
    student_b, _ = database.seed_student("tenant_b", "Bea")
    app = create_app(lambda: _ScopedConnection(database), run_migrations=False)

    with TestClient(app) as client:
        response = client.get(
            f"/v1/sync/snapshot?student_id={student_b}",
            headers={"Authorization": f"Bearer {token_a}"},
        )

    assert response.status_code in (403, 404)
    assert student_a != student_b


def test_sync_body_student_mismatch_returns_403() -> None:
    database = _ScopedDatabase()
    _, token = database.seed_student("tenant_a", "Ari")
    other_student, _ = database.seed_student("tenant_a", "Bea")
    app = create_app(lambda: _ScopedConnection(database), run_migrations=False)

    with TestClient(app) as client:
        response = client.post(
            "/v1/sync/events",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "device_id": "device-a",
                "student_id": other_student,
                "events": [],
            },
        )

    assert response.status_code == 403
    assert response.json()["detail"] == "Student scope mismatch"


def test_knowledge_endpoint_uses_request_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _ScopedDatabase()
    connections: list[_ScopedConnection] = []
    backend_connections: list[_ScopedConnection] = []
    closed_during_retrieve: list[bool] = []

    class _FakeKnowledgeBackend:
        def __init__(self, connection: _ScopedConnection) -> None:
            backend_connections.append(connection)
            self.connection = connection

        def retrieve(self, query: str, **kwargs: Any) -> RetrievalResponse:
            closed_during_retrieve.append(self.connection.closed)
            return RetrievalResponse(explicit_no_result=True)

    monkeypatch.setattr(knowledge_router, "KnowledgeBackend", _FakeKnowledgeBackend)

    def connection_factory() -> _ScopedConnection:
        connection = _ScopedConnection(database)
        connections.append(connection)
        return connection

    app = create_app(connection_factory, run_migrations=False)
    with TestClient(app) as client:
        response = client.post(
            "/v1/knowledge/retrieve",
            json={"query": "linear equations"},
        )

    assert response.status_code == 200
    assert len(connections) == 1
    assert backend_connections == connections
    assert closed_during_retrieve == [False]
    assert connections[0].closed is True


def test_sync_service_dependency_uses_typed_request_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected_connection = object()
    request = SimpleNamespace(state=SimpleNamespace(connection=object()))

    class _RecordingService:
        def __init__(self, connection: object) -> None:
            self.connection = connection

    monkeypatch.setattr(sync_router_module, "SyncService", _RecordingService)
    monkeypatch.setattr(
        sync_router_module,
        "request_connection",
        lambda _: expected_connection,
    )

    service = sync_router_module.get_service(request)

    assert service.connection is expected_connection


def test_knowledge_dependency_uses_typed_request_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected_connection = object()
    request = SimpleNamespace(state=SimpleNamespace(connection=object()))
    backend_connections: list[object] = []

    class _RecordingBackend:
        def __init__(self, connection: object) -> None:
            backend_connections.append(connection)

    monkeypatch.setattr(knowledge_router_module, "KnowledgeBackend", _RecordingBackend)
    monkeypatch.setattr(
        knowledge_router_module,
        "request_connection",
        lambda _: expected_connection,
    )

    knowledge_router_module.get_backend(request)

    assert backend_connections == [expected_connection]


@pytest.fixture()
def pg_api_app(pg_app):
    return pg_app


def test_authenticated_sync_request_reaches_service_under_rls(
    pg_api_app, pg_tenant: str
) -> None:
    with TestClient(pg_api_app) as client:
        created = client.post(
            "/v1/students",
            json={"name": "Ari", "daily_minutes": 15, "target_score": 1100},
        )
        assert created.status_code == 201
        student = created.json()

        response = client.post(
            "/v1/sync/devices",
            headers={"Authorization": f"Bearer {student['token']}"},
            json={"device_name": "phone"},
        )

    assert response.status_code == 201
    assert response.json()["student_id"] == student["id"]

    admin = pg.connect_admin()
    try:
        row = admin.execute(
            "SELECT tenant_id, student_id FROM devices WHERE device_id = %s",
            (response.json()["device_id"],),
        ).fetchone()
    finally:
        pg.quiet_close(admin)

    assert row == {"tenant_id": pg_tenant, "student_id": student["id"]}
