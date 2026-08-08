"""PG 连接层测试。需要本地 PG 已启动(scripts/dev_env.py up)。"""
from __future__ import annotations

import pytest

from app.infrastructure.pg import connect, connect_admin, database_version, transaction


@pytest.fixture(scope="module")
def pg_conn():
    conn = connect()
    yield conn
    conn.close()


@pytest.fixture()
def admin_conn():
    conn = connect_admin()
    yield conn
    conn.close()


def test_connect_returns_row_dict(pg_conn) -> None:
    row = pg_conn.execute("SELECT 1 AS one").fetchone()
    assert row["one"] == 1


def test_connect_runs_cleanup_sql(pg_conn) -> None:
    row = pg_conn.execute("SELECT current_setting('search_path') AS sp").fetchone()
    assert row["sp"] == "public"


def test_database_version_zero_on_unmigrated_db(admin_conn) -> None:
    conn = admin_conn
    try:
        conn.execute("DROP TABLE IF EXISTS schema_migrations")
        assert database_version(conn) == 0
        row = conn.execute("SELECT 1 AS one").fetchone()
        assert row["one"] == 1
    finally:
        conn.rollback()
        conn.close()


def test_transaction_commits_across_connections(admin_conn) -> None:
    table_name = "txn_commit_probe"
    admin_conn.execute(f"DROP TABLE IF EXISTS {table_name}")
    with transaction(admin_conn):
        admin_conn.execute(f"CREATE TABLE {table_name} (v INTEGER)")
    other = connect_admin()
    try:
        row = other.execute(
            "SELECT COUNT(*) AS n FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = %s",
            (table_name,),
        ).fetchone()
        assert row["n"] == 1
    finally:
        other.execute(f"DROP TABLE IF EXISTS {table_name}")
        other.commit()
        other.close()


def test_transaction_rolls_back_on_error(admin_conn) -> None:
    table_name = "txn_rollback_probe"
    admin_conn.execute(f"DROP TABLE IF EXISTS {table_name}")
    try:
        with pytest.raises(RuntimeError):
            with transaction(admin_conn):
                admin_conn.execute(f"CREATE TABLE {table_name} (v INTEGER)")
                raise RuntimeError("boom")
        row = admin_conn.execute(
            "SELECT COUNT(*) AS n FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = %s",
            (table_name,),
        ).fetchone()
        assert row["n"] == 0
    finally:
        admin_conn.execute(f"DROP TABLE IF EXISTS {table_name}")
