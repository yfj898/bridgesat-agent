"""双角色架构下 RLS 真正生效的测试。

bridgesat(超级用户)只用于迁移;bridgesat_app 是运行时角色,是 RLS 的
隔离主体。这些测试证明:app 角色默认什么都看不到(fail closed)、只能
看到自己租户的行、不能插入其他租户的行,并且 resolve_token 能绕过
未设置的 app.tenant_id 精确解析 token。
"""
from __future__ import annotations

import pytest

from app.infrastructure import pg
from app.infrastructure.migration_runner import migrate_database


@pytest.fixture()
def migrated():
    """Fresh migrated database via the superuser connection."""
    admin = pg.connect_admin()
    migrate_database(admin)
    yield admin
    admin.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    admin.commit()
    admin.close()


def _insert_student(conn, student_id: str, tenant_id: str) -> None:
    conn.execute(
        "INSERT INTO students (id, tenant_id, name, daily_minutes, target_score, mastery_json) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (student_id, tenant_id, "Alice", 30, 90, "{}"),
    )


def test_app_role_fails_closed_without_tenant(migrated) -> None:
    _insert_student(migrated, "stu_1", "tenant_a")
    migrated.commit()

    app = pg.connect()
    try:
        rows = app.execute("SELECT id FROM students").fetchall()
        assert rows == []
    finally:
        app.close()


def test_app_role_sees_only_its_own_tenant_rows(migrated) -> None:
    _insert_student(migrated, "stu_1", "tenant_a")
    _insert_student(migrated, "stu_2", "tenant_b")
    migrated.commit()

    app = pg.connect()
    try:
        app.execute("SET app.tenant_id = 'tenant_a'")
        rows = app.execute("SELECT id FROM students ORDER BY id").fetchall()
        assert [row["id"] for row in rows] == ["stu_1"]

        app.execute("SET app.tenant_id = 'tenant_b'")
        rows = app.execute("SELECT id FROM students ORDER BY id").fetchall()
        assert [row["id"] for row in rows] == ["stu_2"]
    finally:
        app.close()


def test_app_role_cannot_insert_other_tenants_row(migrated) -> None:
    app = pg.connect()
    try:
        app.execute("SET app.tenant_id = 'tenant_a'")
        with pytest.raises(Exception, match="row-level security"):
            _insert_student(app, "stu_other", "tenant_b")
    finally:
        app.close()


def test_resolve_token_bypasses_rls_for_app_role(migrated) -> None:
    migrated.execute(
        "INSERT INTO student_tokens "
        "(token_id, tenant_id, student_id, token_hash, created_at) "
        "VALUES (%s, %s, %s, %s, %s)",
        ("tok_1", "tenant_a", "stu_1", "abc123hash", "2026-01-01"),
    )
    migrated.commit()

    app = pg.connect()
    try:
        rows = app.execute("SELECT * FROM resolve_token('abc123hash')").fetchall()
        assert len(rows) == 1
        assert rows[0]["tenant_id"] == "tenant_a"
        assert rows[0]["student_id"] == "stu_1"
    finally:
        app.close()
