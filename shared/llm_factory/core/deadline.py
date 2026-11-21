"""The time a user request has left, carried to every LLM call it makes.

A template's /search and /answer run under ``request_deadline(budget)``. Inside it,
every LLM call is bounded twice:

- by the request: an attempt, a wait for a busy provider and a wait for a free
  concurrency slot all get only the time that is left;
- per call: one LLM call made while a user waits may take ``LLM_QUERY_TIMEOUT_SECONDS``
  in all (attempts and busy waits together), never the background
  ``LLM_REQUEST_TIMEOUT_SECONDS`` plus its 180 s busy wait.

When the time is gone, the call raises ``LLMBudgetSpent``. Background work (ingest,
compile, judge, the markdown converter) never sets a deadline, so nothing here
changes it.

``LLMBudgetSpent`` is a BaseException, like asyncio.CancelledError: the search
strategies degrade on any ``Exception`` from an LLM call (HyDE falls back to the
question, a reranker to the scores), which is right for a failed call and wrong for
a request that is out of time. It has to reach the router, which answers 504.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import AsyncIterator, Callable, Iterator, Optional, TypeVar

from .generation import query_timeout

T = TypeVar("T")

# Monotonic time at which the current request is out of time; None = no deadline.
_deadline: ContextVar[Optional[float]] = ContextVar("llm_request_deadline", default=None)


class LLMBudgetSpent(BaseException):
    """The user request this call belongs to has no time left."""


class LLMCallTimeout(TimeoutError):
    """One attempt had no complete answer within its deadline. A timeout like any
    other: a page call (call_sync) retries it as transient, a text call does not.
    Also what one LLM call on a user request raises when it used up its own
    LLM_QUERY_TIMEOUT_SECONDS while the request still has time: the caller may
    degrade (HyDE falls back to the question), as for any failed call."""


def time_left() -> Optional[float]:
    """Seconds the current request has left, or None when there is no deadline."""
    deadline = _deadline.get()
    return None if deadline is None else deadline - time.monotonic()


def has_deadline() -> bool:
    return _deadline.get() is not None


@contextmanager
def request_deadline(seconds: float) -> Iterator[None]:
    """Run the block with at most ``seconds`` left. Nested deadlines keep the earlier one.

    An error that leaves the block once the deadline has passed is the request running
    out of time, whatever raised it (a database statement cut by its limit, a pool wait,
    an HTTP read): it leaves as LLMBudgetSpent, so the router reports the 504 timeout
    message instead of a generic failure."""
    new = time.monotonic() + seconds
    current = _deadline.get()
    end = new if current is None else min(current, new)
    token = _deadline.set(end)
    try:
        yield
    except Exception as exc:
        if time.monotonic() >= end:
            raise LLMBudgetSpent(f"request budget spent ({type(exc).__name__}: {exc})") from exc
        raise
    finally:
        _deadline.reset(token)


async def stream_within_deadline(events: AsyncIterator[T], seconds: float) -> AsyncIterator[T]:
    """Iterate ``events`` with at most ``seconds`` for all of it.

    The deadline is set around each step only (the producer's work up to its next
    item) and reset in the same frame. Held across the yields instead, it would also
    bound the consumer's code between items (Starlette's sends), and a stream closed
    from another context (a client gone, the loop's asyncgen finalizer) would fail to
    reset it ("created in a different Context") and hide the real cancellation."""
    end = time.monotonic() + seconds
    it = events.__aiter__()
    try:
        while True:
            with request_deadline(end - time.monotonic()):
                try:
                    item = await it.__anext__()
                except StopAsyncIteration:
                    return
            yield item
    finally:
        aclose = getattr(it, "aclose", None)
        if aclose is not None:
            await aclose()


@contextmanager
def llm_call_scope() -> Iterator[None]:
    """One LLM call on a user request: at most LLM_QUERY_TIMEOUT_SECONDS in all.
    No-op outside a request (background work keeps its own limits)."""
    outer = _deadline.get()
    if outer is None:
        yield
        return
    try:
        with request_deadline(query_timeout()):
            yield
    except LLMBudgetSpent as exc:
        if time.monotonic() < outer:  # the call's own time ran out, not the request's
            raise LLMCallTimeout(f"{exc} (the LLM call's own time, LLM_QUERY_TIMEOUT_SECONDS)") from exc
        raise


def cap(seconds: float, what: str = "llm call") -> float:
    """``seconds``, cut to the time the request has left. Raises LLMBudgetSpent when
    none is left. Unchanged outside a request."""
    left = time_left()
    if left is None:
        return seconds
    if left <= 0:
        raise LLMBudgetSpent(f"{what}: request budget spent")
    return min(seconds, left)


def spent() -> bool:
    """True when the current request has a deadline and it has passed."""
    left = time_left()
    return left is not None and left <= 0


async def within_time_left(aw, what: str = "request"):
    """``await aw``, cut when the current request runs out of time (LLMBudgetSpent).

    For work whose LLM calls may not see the request's deadline: a library that runs
    them in worker tasks it started before the request (LightRAG). No deadline set:
    simply awaited."""
    import asyncio

    left = time_left()
    if left is None:
        return await aw
    if left <= 0:
        raise LLMBudgetSpent(f"{what}: request budget spent")
    try:
        return await asyncio.wait_for(aw, left)
    except asyncio.TimeoutError as exc:
        raise LLMBudgetSpent(f"{what}: request budget spent") from exc


# ── Progress of background work ────────────────────────────────────────────────
# An ingest run listens here: every LLM or embedding call that completes inside it is
# progress (shared/rag_core/ingest_run.py). A run whose calls all stopped completing
# is stalled even while its process heartbeats. The listener may raise to stop the
# work (a cancel asked for mid-file).

_progress: ContextVar[Optional[Callable[[], None]]] = ContextVar("llm_progress_listener", default=None)


def note_progress() -> None:
    """Tell the work in progress (if it listens) that a call just completed."""
    listener = _progress.get()
    if listener is not None:
        listener()


@contextmanager
def progress_listener(listener: Callable[[], None]) -> Iterator[None]:
    """Run the block with ``listener`` told of every completed LLM/embedding call."""
    token = _progress.set(listener)
    try:
        yield
    finally:
        _progress.reset(token)
