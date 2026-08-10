"""Tenant-isolated PostgreSQL memory outbox dispatch."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import psycopg

from .worker import OutboxWorker


ConnectionFactory = Callable[[], psycopg.Connection]
IndexFactory = Callable[[psycopg.Connection, str], Any]
MAX_RECORDED_ERROR_LENGTH = 500


class TenantOutboxDispatcher:
    """Dispatch one bounded outbox batch at a time for every known tenant.

    The admin connection is deliberately limited to tenant discovery. All
    outbox reads and writes happen on a newly-created application connection
    after its tenant setting has been committed, so RLS remains the boundary
    for both delivery and derived-index calls.
    """

    def __init__(
        self,
        admin_connection: psycopg.Connection,
        app_connection_factory: ConnectionFactory,
        index_factory: IndexFactory | None,
    ) -> None:
        self.admin_connection = admin_connection
        self.app_connection_factory = app_connection_factory
        self.index_factory = index_factory
        self.last_errors: dict[str, str] = {}
        self.processed_tenants: list[str] = []

    def tenant_ids(self) -> list[str]:
        """Return tenants with outbox rows a worker can still claim."""
        try:
            rows = self.admin_connection.execute(
                "SELECT DISTINCT tenant_id FROM memory_outbox "
                "WHERE tenant_id IS NOT NULL "
                "AND status IN ('pending', 'retrying', 'deletion_pending', 'processing') "
                "ORDER BY tenant_id"
            ).fetchall()
            return [str(row["tenant_id"]) for row in rows]
        finally:
            # A discovery SELECT otherwise leaves the long-lived admin
            # connection in an open transaction between poll iterations.
            self.admin_connection.rollback()

    def run_once(self) -> int:
        """Process one batch per tenant; return successful deliveries."""
        self._reset_run_state()
        if self.index_factory is None:
            return 0

        processed = 0
        for tenant_id in self.tenant_ids():
            connection: psycopg.Connection | None = None
            tenant_error: BaseException | None = None
            try:
                connection = self._tenant_connection(tenant_id)
                index = self.index_factory(connection, tenant_id)
                worker = OutboxWorker(connection, index=index)
                worker.run_pending()
                processed += worker.successful_total
                self._record_worker_failures(tenant_id, worker)
            except Exception as exc:
                tenant_error = exc
                self.last_errors[tenant_id] = self._error_message(exc)
            finally:
                cleanup_error = self._cleanup_connection(connection)
                if tenant_error is None and cleanup_error is not None:
                    self.last_errors[tenant_id] = self._error_message(cleanup_error)

            if tenant_id not in self.last_errors:
                self.processed_tenants.append(tenant_id)
        return processed

    async def run_pending_async(self) -> int:
        """Run the synchronous bounded batch off the event-loop thread.

        A cancellation is remembered but not delivered to the caller until
        the worker thread finishes, so lifespan shutdown cannot close the
        admin connection while ``run_once`` is still using it. The result is
        the same successful-delivery count as ``run_once``.
        """
        thread_task = asyncio.create_task(asyncio.to_thread(self.run_once))
        cancelled = False
        while True:
            try:
                result = await asyncio.shield(thread_task)
            except asyncio.CancelledError:
                if thread_task.cancelled():
                    raise
                cancelled = True
                continue
            except Exception:
                if cancelled:
                    raise asyncio.CancelledError
                raise
            if cancelled:
                raise asyncio.CancelledError
            return result

    def close(self) -> None:
        """Rollback and close the long-lived admin connection."""
        try:
            self.admin_connection.rollback()
        finally:
            self.admin_connection.close()

    def _tenant_connection(self, tenant_id: str) -> psycopg.Connection:
        connection = self.app_connection_factory()
        try:
            connection.execute(
                "SELECT set_config('app.tenant_id', %s, false)",
                (tenant_id,),
            )
            connection.commit()
            return connection
        except Exception:
            # The caller cannot clean up a connection that was never
            # returned, so setup failures must close it at this boundary.
            self._cleanup_connection(connection)
            raise

    @staticmethod
    def _cleanup_connection(
        connection: psycopg.Connection | None,
    ) -> Exception | None:
        if connection is None:
            return None

        cleanup_error: Exception | None = None
        try:
            connection.rollback()
        except Exception as exc:
            cleanup_error = exc
        try:
            connection.close()
        except Exception as exc:
            if cleanup_error is None:
                cleanup_error = exc
        return cleanup_error

    @staticmethod
    def _error_message(error: BaseException) -> str:
        message = str(error) or error.__class__.__name__
        return message[:MAX_RECORDED_ERROR_LENGTH]

    def _reset_run_state(self) -> None:
        self.last_errors = {}
        self.processed_tenants = []

    def _record_worker_failures(self, tenant_id: str, worker: OutboxWorker) -> None:
        if not getattr(worker, "failed_total", 0):
            return
        errors = getattr(worker, "last_errors", {})
        error = next(iter(errors.values()), "outbox delivery failed")
        self.last_errors[tenant_id] = str(error)[:MAX_RECORDED_ERROR_LENGTH]
