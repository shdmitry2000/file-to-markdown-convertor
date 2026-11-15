"""Small async helpers shared across services."""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import logging
import threading
import typing as t

# One loop for every synchronous caller, running forever on its own thread. This
# used to be ``asyncio.run`` per call: a throwaway loop for each RAGAS question,
# which litellm's module-level primitives re-bind to on every call, and which
# stranded each per-call HTTP client's connection pool on a loop that was closed.
_LOOP: t.Optional[asyncio.AbstractEventLoop] = None
_LOOP_LOCK = threading.Lock()
logger = logging.getLogger(__name__)


def _shared_loop() -> asyncio.AbstractEventLoop:
    global _LOOP
    with _LOOP_LOCK:
        if _LOOP is None or _LOOP.is_closed():
            _LOOP = asyncio.new_event_loop()
            threading.Thread(target=_LOOP.run_forever, name="run-async-loop", daemon=True).start()
        return _LOOP


def run_async(coro: t.Awaitable[t.Any]) -> t.Any:
    """Run an async coroutine from sync code, even if a loop is already running.

    The coroutine runs on one process-wide loop on a dedicated thread, so the
    caller's loop (if any) is neither blocked nor nested, and every call reaches
    the same loop. Only loop *detection* sits inside the try: an error raised by
    the coroutine itself propagates as-is.
    """
    loop = _shared_loop()
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is loop:
        # Sync code called from a coroutine already on the shared loop: waiting on
        # that loop from its own thread would deadlock, so this rare case keeps the
        # old one-off loop on a worker thread. It blocks the shared loop for the whole
        # call, so it is logged: the caller should use the async API instead (RAGAS
        # does once its LLM says is_async).
        logger.warning("run_async called from a coroutine on its own loop: %s blocks every "
                       "other caller until it returns — use the async API", _name(coro))
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(contextvars.copy_context().run, asyncio.run, coro).result()
    # The caller's context variables (the request's space, its resolved connector)
    # travel with the coroutine, as they did with asyncio.run; the shared loop's
    # thread has its own context and would otherwise see none of them.
    caller = list(contextvars.copy_context().items())
    return asyncio.run_coroutine_threadsafe(_with_context(caller, coro), loop).result()


async def _with_context(caller: list, coro: t.Awaitable[t.Any]) -> t.Any:
    # Set inside this task, whose context is its own copy: nothing leaks to the loop.
    for var, value in caller:
        var.set(value)
    return await coro


def _name(coro: t.Awaitable[t.Any]) -> str:
    return getattr(coro, "__qualname__", None) or type(coro).__name__
