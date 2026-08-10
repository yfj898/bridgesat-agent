from __future__ import annotations

import pytest

from app.auth import TokenStore, resolve_tenant
from app.infrastructure import pg
from app.infrastructure.migration_runner import migrate_database


TENANT = "tenant_test"


@pytest.fixture()
def store():
    admin = pg.connect_admin()
    migrate_database(admin)
    admin.close()

    connection = pg.connect()
    try:
        connection.execute(
            "SELECT set_config('app.tenant_id', %s, false)",
            (TENANT,),
        )
        connection.execute(
            """
            INSERT INTO students (
                id, tenant_id, name, daily_minutes, target_score,
                mastery_json, status, created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, 'active', %s, %s)
            """,
            (
                "stu_auth",
                TENANT,
                "Auth Student",
                15,
                1100,
                "{}",
                "2026-01-01",
                "2026-01-01",
            ),
        )
        connection.commit()
        yield TokenStore(connection)
    finally:
        connection.close()
        cleanup = pg.connect_admin()
        try:
            cleanup.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
            cleanup.commit()
        finally:
            cleanup.close()


def test_issue_resolve_round_trip_and_verify(store: TokenStore) -> None:
    token = store.issue("stu_auth")

    assert store.resolve(token) == "stu_auth"
    assert store.verify("stu_auth", token) is True


def test_revoke_makes_token_unresolvable(store: TokenStore) -> None:
    token = store.issue("stu_auth")

    store.revoke(token)

    assert store.resolve(token) is None


def test_resolve_works_from_another_tenant_context(store: TokenStore) -> None:
    token = store.issue("stu_auth")
    store.connection.execute(
        "SELECT set_config('app.tenant_id', %s, false)",
        ("tenant_other",),
    )
    store.connection.commit()

    assert store.resolve(token) == "stu_auth"


def test_resolve_tenant_returns_tenant_and_student(store: TokenStore) -> None:
    token = store.issue("stu_auth")

    assert resolve_tenant(store, token) == (TENANT, "stu_auth")
