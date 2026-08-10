"""Memory index factory tests with request-scoped PostgreSQL connections."""

from __future__ import annotations

import psycopg
import pytest

from app.infrastructure import pg
from app.infrastructure.migration_runner import migrate_database
from app.memory import build_mnemis_index
from app.memory.nvidia_backend import NvidiaMemoryIndex
from tests.pg_test_helpers import cleanup_tenant, unique_tenant_id


@pytest.fixture()
def connection() -> psycopg.Connection:
    admin = pg.connect_admin()
    migrate_database(admin)
    admin.close()
    tenant_id = unique_tenant_id("task3_factory")
    conn = pg.connect()
    conn.execute(
        "SELECT set_config('app.tenant_id', %s, false)",
        (tenant_id,),
    )
    conn.commit()
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()
        cleanup = pg.connect_admin()
        try:
            cleanup_tenant(cleanup, tenant_id)
        finally:
            cleanup.close()


def test_without_llm_key_returns_default_adapter(
    connection: psycopg.Connection, monkeypatch
) -> None:
    monkeypatch.delenv("BRIDGESAT_LLM_API_KEY", raising=False)
    adapter = build_mnemis_index(connection)
    from app.memory.mnemis_backend import MnemisMemoryAdapter

    assert isinstance(adapter, MnemisMemoryAdapter)
    assert adapter.api_key == ""
    assert adapter._transport.__name__ == "_unconfigured_transport"


def test_with_llm_key_returns_nvidia_index_adapter(
    connection: psycopg.Connection, monkeypatch
) -> None:
    monkeypatch.setenv("BRIDGESAT_LLM_API_KEY", "nvapi-test-key")
    adapter = build_mnemis_index(connection)
    from app.memory.mnemis_backend import MnemisMemoryAdapter

    assert isinstance(adapter, MnemisMemoryAdapter)
    assert isinstance(adapter._transport, NvidiaMemoryIndex)
    assert adapter._transport.connection is connection
    assert adapter.base_url == "http://local/nvidia-index"
