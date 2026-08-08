"""PG 连接层测试。需要本地 PG 已启动(scripts/dev_env.py up)。"""
from __future__ import annotations

import pytest

from app.infrastructure.pg import connect, transaction

DSN = "postgresql://bridgesat:bridgesat@localhost:5432/bridgesat"


@pytest.fixture(scope="module")
def pg_conn():
    conn = connect(DSN)
    yield conn
    conn.close()


def test_connect_returns_row_dict(pg_conn) -> None:
    row = pg_conn.execute("SELECT 1 AS one").fetchone()
    assert row["one"] == 1


def test_connect_runs_cleanup_sql(pg_conn) -> None:
    row = pg_conn.execute("SELECT current_setting('search_path') AS sp").fetchone()
    assert row["sp"] == "public"


def test_transaction_commits(pg_conn) -> None:
    with transaction(pg_conn):
        pg_conn.execute(
            "CREATE TEMP TABLE txn_probe (v INTEGER); INSERT INTO txn_probe VALUES (7)"
        )
    row = pg_conn.execute("SELECT COUNT(*) AS n FROM txn_probe").fetchone()
    assert row["n"] == 1


def test_transaction_rolls_back_on_error(pg_conn) -> None:
    with pytest.raises(RuntimeError):
        with transaction(pg_conn):
            pg_conn.execute(
                "CREATE TEMP TABLE txn_probe2 (v INTEGER); INSERT INTO txn_probe2 VALUES (1)"
            )
            raise RuntimeError("boom")
    row = pg_conn.execute(
        "SELECT COUNT(*) AS n FROM information_schema.tables "
        "WHERE table_name = 'txn_probe2'"
    ).fetchone()
    assert row["n"] == 0
