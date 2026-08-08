"""Await a coroutine from synchronous code, in any context."""

from __future__ import annotations

import asyncio
import threading


def await_in_any_context(coro):
    """Resolve a coroutine from a sync call path.

    Outside a running event loop ``asyncio.run`` is correct; inside one (e.g.
    an async route that delegates to a sync engine function) the coroutine is
    run on a fresh loop in a worker thread so the call still resolves without
    blocking the running loop.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    result: dict = {}

    def _run_in_thread() -> None:
        result["value"] = asyncio.run(coro)

    thread = threading.Thread(target=_run_in_thread, daemon=True)
    thread.start()
    thread.join()
    return result["value"]
