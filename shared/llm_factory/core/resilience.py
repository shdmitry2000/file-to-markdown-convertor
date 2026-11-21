"""The one place an LLM call waits out a busy provider, and the one cap on calls in flight.

Every generation method of the concrete clients (text, json, stream) is wrapped
here, so every caller gets the same behaviour whichever lane it is on and however
it built its client. Callers do not retry rate limits themselves.

Before this, eight places each detected and retried 429s their own way (answer
4 attempts, judge 4, enrichers 5, navigate 3, wiki 5, converter 8, ...) and
disagreed on what counted as one. On 2026-10-29 three RAGs evaluated at once on
gke-dev with an unbounded judge lost a third of their answers and scores to
Vertex RESOURCE_EXHAUSTED.

Setting (one for every module):

- ``LLM_MAX_CONCURRENCY`` (default 8): generation calls in flight per process.
  8 is measured, on gemini-3.5-flash: four to five busy pods at 8 each is the load
  that ran with no 429s on gke-dev. Google publishes no number for its shared
  quota; slower models (Pro) likely want ~4. 0 = no cap.

Constants (settings until 1.4.8.4, never set to anything but these):

- ``MAX_WAIT_SECONDS`` (180): total time one call may spend waiting between
  attempts. The callers' own deadlines around a single call (answer 300 s, judge
  300 s, wiki compile 300 s) are above it, so a call that only waited out 429s says
  "rate limited", not "timed out". At 220, a local wiki compile that waited out
  216 s of 429s then timed out mid-call.
- ``BASE_SECONDS`` (5): first wait, then 10 s per wait, with jitter; a provider's
  Retry-After is followed up to the same 10 s. Until 1.4.8.6 one wait could reach
  30 s (37 s with jitter): an ingest that hit 429s spent most of its time asleep.

What is retried: 429 / RESOURCE_EXHAUSTED and 503 / UNAVAILABLE ("model
overloaded"). A daily cap is not — waiting minutes cannot lift it — and raises
LLMDailyLimitError at once.

Page calls (vision_sync, the markdown converter) also retry a timeout, a 5xx or a
dropped connection twice, briefly (TRANSIENT_WAITS), so one bad page does not fail
a whole document. Text calls do not: their callers' 300 s deadlines hold only for
the busy wait plus ONE request. Worst case for one page, at markdown-api's
LLM_REQUEST_TIMEOUT_SECONDS of 120: 3 x 120 + 3 + 180 = 543 s, under the
converter's 600 s stall cut.

Not retried here: embeddings (the proxy lane retries them per sub-batch, which a
whole-call retry would repeat).

Every attempt has a hard deadline: the call's own timeout, else the lane's
LLM_REQUEST_TIMEOUT_SECONDS. The HTTP clients' timeout is per read, not per request,
so an endpoint that keeps sending a byte now and then (a proxy keeping the
connection alive) held a call open forever, and with it the ingest run that made
it. A stream gets the same deadline for the whole stream.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import random
import re
import threading
import time
from contextvars import ContextVar, copy_context

from .deadline import (  # noqa: F401 — LLMCallTimeout is imported from here by callers
    LLMBudgetSpent, LLMCallTimeout, cap, has_deadline, llm_call_scope, note_progress, spent,
    time_left,
)
from .generation import query_timeout

logger = logging.getLogger(__name__)

MAX_WAIT_SECONDS = 180.0
BASE_SECONDS = 5.0
_MAX_SINGLE_WAIT_SECONDS = 10.0
TRANSIENT_ATTEMPTS = 3
TRANSIENT_WAITS = (1.0, 2.0)


class LLMRateLimitError(Exception):
    """The provider stayed busy (429/503) for the whole wait budget.

    ``status_code`` is the provider's; ``retry_after`` its last hint, if any.
    """

    def __init__(self, message: str, status_code: int = 429, retry_after: float | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


class LLMDailyLimitError(RuntimeError):
    """The lane's per-day token cap is spent: no wait within a request lifts it.

    Not an LLMRateLimitError, so no outer caller treats it as "busy, try later"."""


# ── What counts as "busy" ──────────────────────────────────────────────────────

# Vertex's status names are upper case; "unavailable in this region" is not busy.
_BUSY_STATUS = re.compile(r"RESOURCE_EXHAUSTED|\bUNAVAILABLE\b")
_BUSY_TEXT = re.compile(r"HTTP 429\b|HTTP 503\b|overloaded|high demand", re.IGNORECASE)
_DAILY_CAP = re.compile(r"per day|daily", re.IGNORECASE)


def _chain(exc: BaseException):
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        yield exc
        exc = exc.__cause__ or exc.__context__


def _status(exc: BaseException) -> int | None:
    for e in _chain(exc):
        status = getattr(e, "status_code", None) or getattr(getattr(e, "response", None), "status_code", None)
        if isinstance(status, int):
            return status
    return None


def is_rate_limited(exc: BaseException) -> bool:
    """True when the provider said "busy, try later": 429/503 by status, Vertex's
    RESOURCE_EXHAUSTED/UNAVAILABLE, or the client's own RateLimit/ServiceUnavailable
    class. A daily cap is not: no wait within a request lifts it.

    Not the word "quota": "no quota project set" is an auth error."""
    if isinstance(exc, LLMRateLimitError):
        return True
    text = " ".join(str(e) for e in _chain(exc))
    if _DAILY_CAP.search(text):
        return False
    if _status(exc) in (429, 503):
        return True
    for e in _chain(exc):
        name = type(e).__name__
        if "RateLimit" in name or "ServiceUnavailable" in name:
            return True
    return bool(_BUSY_STATUS.search(text) or _BUSY_TEXT.search(text))


def is_daily_cap(exc: BaseException) -> bool:
    """A rate limit (429 / RESOURCE_EXHAUSTED) whose text names a per-day cap."""
    text = " ".join(str(e) for e in _chain(exc))
    if not _DAILY_CAP.search(text):
        return False
    return (_status(exc) == 429 or _http_status_in_text(text) == 429
            or "RESOURCE_EXHAUSTED" in text
            or any("RateLimit" in type(e).__name__ for e in _chain(exc)))


_HTTP_STATUS = re.compile(r"\bHTTP (\d{3})\b")


def _http_status_in_text(text: str) -> int | None:
    """The proxy lane raises a plain RuntimeError ("HTTP 500 for '<url>' ..."):
    only "HTTP nnn" counts, never digits elsewhere in the URL."""
    m = _HTTP_STATUS.search(text)
    return int(m.group(1)) if m else None


def is_transient(exc: BaseException) -> bool:
    """A timeout, a 5xx other than 503 (busy), or a dropped connection: another
    attempt may well succeed. Not a 4xx, not a plain error such as a missing
    credential."""
    if is_rate_limited(exc) or is_daily_cap(exc):
        return False
    status = _status(exc)
    if status is None:
        status = _http_status_in_text(" ".join(str(e) for e in _chain(exc)))
    if status is not None:
        return status in (408, 500, 502, 504)
    return any("Timeout" in type(e).__name__ or "Connection" in type(e).__name__
               for e in _chain(exc))


def retry_after(exc: BaseException) -> float | None:
    """The provider's Retry-After, in seconds, when it sent one."""
    for e in _chain(exc):
        headers = getattr(getattr(e, "response", None), "headers", None) or {}
        try:
            value = headers.get("retry-after") or headers.get("Retry-After")
        except AttributeError:
            continue
        if not value:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    return None


# ── Settings ───────────────────────────────────────────────────────────────────

def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("%s=%r is not a number; using %s", name, raw, default)
        return default


def max_wait_seconds() -> float:
    return MAX_WAIT_SECONDS


# ── Concurrency cap ────────────────────────────────────────────────────────────

class _Limiter:
    """A process-wide cap shared by async callers (any event loop) and sync callers
    (worker threads). A threading semaphore, polled from async code: an
    asyncio.Semaphore is bound to one loop, and this process runs several."""

    def __init__(self, limit: int):
        self.limit = limit
        self._sem = threading.BoundedSemaphore(limit) if limit > 0 else None

    def acquire_sync(self) -> None:
        if self._sem is None:
            return
        left = time_left()
        if left is None:
            self._sem.acquire()
        elif left <= 0 or not self._sem.acquire(timeout=left):
            raise LLMBudgetSpent("waiting for a free LLM slot: request budget spent")

    async def acquire_async(self) -> None:
        if self._sem is None:
            return
        pause = 0.01
        while not self._sem.acquire(blocking=False):
            left = time_left()
            if left is not None and left <= 0:
                raise LLMBudgetSpent("waiting for a free LLM slot: request budget spent")
            await asyncio.sleep(pause if left is None else min(pause, left))
            pause = min(pause * 2, 0.25)

    def release(self) -> None:
        if self._sem is not None:
            self._sem.release()


_limiter: _Limiter | None = None
_limiter_lock = threading.Lock()


def limiter() -> _Limiter:
    global _limiter
    with _limiter_lock:
        if _limiter is None:
            _limiter = _Limiter(int(_env_float("LLM_MAX_CONCURRENCY", 8)))
        return _limiter


def max_concurrency() -> int:
    """LLM_MAX_CONCURRENCY, for a caller that sizes a thread pool by it (more threads
    than slots would only wait); 8 when the cap is off (0)."""
    return limiter().limit or 8


def reset_limiter() -> None:
    """Re-read LLM_MAX_CONCURRENCY (tests)."""
    global _limiter
    with _limiter_lock:
        _limiter = None


# A client method that calls another wrapped method (GeminiClient.json -> text)
# must not take a second slot or retry twice.
_inside: ContextVar[bool] = ContextVar("llm_call_inside", default=False)


# ── The hard deadline of one attempt ───────────────────────────────────────────



def attempt_seconds(kwargs: dict) -> float:
    """One attempt's deadline: the call's own (``generation.timeout``, a stream's
    ``timeout``), else the lane's LLM_REQUEST_TIMEOUT_SECONDS."""
    from .generation import request_timeout_for  # noqa: PLC0415

    own = kwargs.get("timeout")
    return cap(own) if own else request_timeout_for(kwargs.get("generation"))


async def within_deadline(aw, seconds: float, what: str):
    """``await aw``, ended at ``seconds`` however the endpoint behaves."""
    try:
        result = await asyncio.wait_for(aw, seconds)
    except asyncio.TimeoutError as exc:
        if spent():  # cut by the request's deadline, not by the call's own timeout
            raise LLMBudgetSpent(f"{what}: request budget spent") from exc
        raise LLMCallTimeout(f"{what}: no complete answer within {seconds:.0f}s") from exc
    note_progress()  # an ingest run counts this as progress (core/deadline.py)
    return result


def _sync_within_deadline(fn, args, kwargs, seconds: float, what: str):
    """``fn(*args, **kwargs)`` on a helper thread, given up at ``seconds``. A blocking
    HTTP read cannot be cancelled; the abandoned thread ends when its socket does,
    and the caller is not held by it."""
    box: dict = {}
    done = threading.Event()
    ctx = copy_context()  # _inside and the request's SPACE go with the call

    def run() -> None:
        try:
            box["value"] = ctx.run(fn, *args, **kwargs)
        except BaseException as exc:  # noqa: BLE001 — handed to the caller below
            box["error"] = exc
        finally:
            done.set()

    threading.Thread(target=run, name="llm-call", daemon=True).start()
    if not done.wait(seconds):
        logger.warning("[llm] %s: no complete answer within %.0fs; abandoned", what, seconds)
        if spent():
            raise LLMBudgetSpent(f"{what}: request budget spent")
        raise LLMCallTimeout(f"{what}: no complete answer within {seconds:.0f}s")
    if "error" in box:
        raise box["error"]
    note_progress()
    return box["value"]


# ── The one retry policy ───────────────────────────────────────────────────────

def _log_give_up(what: str, exc: BaseException, attempts: int, waited: float) -> None:
    """One `llm.*` event (shared/log_events.py) for a call that failed for good with a
    cause an operator must act on: provider busy, daily cap, no answer in time, or the
    lane's credentials refused. Other failures (a bad request) stay with the caller."""
    try:
        from shared.log_events import log_event
    except ImportError:  # a module shipped without shared.log_events
        return
    status = _status(exc)
    line = dict(outcome="failed", actor="system", target_type="llm", target_id=what,
                status=status, attempts=attempts + 1, waited_seconds=round(waited),
                error=f"{type(exc).__name__}: {str(exc)[:300]}")
    if is_daily_cap(exc):
        log_event("llm.daily_limit_reached", **line)
    elif is_rate_limited(exc):
        log_event("llm.rate_limited", **line)
    elif isinstance(exc, LLMCallTimeout):
        log_event("llm.timed_out", **line)
    elif status in (401, 403):
        log_event("llm.auth_failed", **line)


class _Attempts:
    """Waits between attempts of one call, until the total budget is spent."""

    def __init__(self, what: str):
        self.what = what
        self.budget = max_wait_seconds()
        self.waited = 0.0
        self.n = 0

    def next_wait(self, exc: BaseException) -> float | None:
        """Seconds to wait before the next attempt, or None to give up."""
        if not is_rate_limited(exc):
            return None
        self.n += 1
        hint = retry_after(exc)
        wait = hint if hint is not None else BASE_SECONDS * 2 ** (self.n - 1) * random.uniform(0.75, 1.25)
        wait = min(wait, _MAX_SINGLE_WAIT_SECONDS)
        remaining = self.budget - self.waited
        if remaining <= 0:
            return None
        wait = min(wait, remaining)
        left = time_left()
        if left is not None and wait >= left:
            return None  # the request would be out of time before the next attempt
        self.waited += wait
        logger.warning("[llm] %s busy (attempt %d, %.0fs of %.0fs waited), retrying in %.0fs: %s",
                       self.what, self.n, self.waited - wait, self.budget, wait, str(exc)[:200])
        return wait

    def give_up(self, exc: BaseException) -> BaseException:
        _log_give_up(self.what, exc, self.n, self.waited)
        if is_daily_cap(exc):
            detail = str(exc)[:300]
            logger.error("[llm] %s: the lane's daily token limit is reached (%s)", self.what, detail)
            return LLMDailyLimitError(
                f"LLM daily token limit reached on this lane; calls work again once it "
                f"resets ({detail})")
        if not is_rate_limited(exc) or isinstance(exc, LLMRateLimitError):
            return exc
        status = _status(exc)
        return LLMRateLimitError(
            f"{self.what}: provider busy after {self.n + 1} attempts over {self.waited:.0f}s: {str(exc)[:300]}",
            status_code=status if status in (429, 503) else 429,
            retry_after=retry_after(exc),
        )


async def call_async(fn, *args, what: str = "llm call", **kwargs):
    """``await fn(*args, **kwargs)`` under the cap, waiting out a busy provider."""
    if _inside.get():
        return await fn(*args, **kwargs)
    with llm_call_scope():
        return await _call_async(fn, *args, what=what, **kwargs)


async def _call_async(fn, *args, what: str, **kwargs):
    attempts = _Attempts(what)
    lim = limiter()
    while True:
        await lim.acquire_async()
        token = _inside.set(True)
        try:
            return await within_deadline(fn(*args, **kwargs), attempt_seconds(kwargs), what)
        except Exception as exc:
            error = exc
        finally:
            _inside.reset(token)
            lim.release()
        wait = attempts.next_wait(error)
        if wait is None:
            raised = attempts.give_up(error)
            if raised is error:
                raise error
            raise raised from error
        await asyncio.sleep(wait)


async def stream_async(open_stream, *args, what: str = "llm stream", **kwargs):
    """Yield from ``open_stream(*args, **kwargs)`` (an async iterator), retrying a busy
    provider until the first piece arrives. After that a failure is raised as is: a
    retry would repeat text the caller has already shown. The slot is held for the
    whole stream."""
    attempts = _Attempts(what)
    lim = limiter()
    # On a user request the whole call (attempts, waits, the stream) gets at most
    # LLM_QUERY_TIMEOUT_SECONDS. Computed here, not with llm_call_scope(): a context
    # variable set inside an async generator would leak into the consumer's frames.
    call_end = time.monotonic() + query_timeout() if has_deadline() else None
    while True:
        await lim.acquire_async()
        started = False
        try:
            # attempt_seconds raises LLMBudgetSpent when the request has no time left:
            # the slot must go back either way, or LLM_MAX_CONCURRENCY such timeouts
            # leave every later call in the process waiting for a slot forever.
            seconds = attempt_seconds(kwargs)
            if call_end is not None:
                seconds = min(seconds, call_end - time.monotonic())
                if seconds <= 0:
                    if spent():
                        raise LLMBudgetSpent(f"{what}: request budget spent")
                    raise LLMCallTimeout(f"{what}: no complete answer within the LLM call's time")
            pieces = open_stream(*args, **kwargs).__aiter__()
        except BaseException:
            lim.release()
            raise
        deadline = time.monotonic() + seconds
        try:
            while True:
                try:
                    piece = await within_deadline(
                        pieces.__anext__(), max(deadline - time.monotonic(), 0.0), what)
                except StopAsyncIteration:
                    return
                started = True
                yield piece
        except Exception as exc:
            if started:
                raise
            error = exc
        finally:
            await pieces.aclose()
            lim.release()
        wait = attempts.next_wait(error)
        if wait is None:
            raised = attempts.give_up(error)
            if raised is error:
                raise error
            raise raised from error
        await asyncio.sleep(wait)


# ── Decorators for the client classes ─────────────────────────────────────────

def resilient(fn):
    """Wrap an async client method (text / json)."""
    @functools.wraps(fn)
    async def wrapper(self, *args, **kwargs):
        return await call_async(fn, self, *args, what=f"{type(self).__name__}.{fn.__name__}", **kwargs)
    return wrapper


def call_sync(fn, *args, what: str = "llm call", **kwargs):
    """``fn(*args, **kwargs)`` under the cap, from a worker thread: waits out a busy
    provider like call_async, and also retries a transient failure (timeout / 5xx /
    dropped connection) TRANSIENT_ATTEMPTS times — page calls only, see the module
    docstring. The slot is released while waiting."""
    if _inside.get():
        return fn(*args, **kwargs)
    with llm_call_scope():
        return _call_sync(fn, *args, what=what, **kwargs)


def _call_sync(fn, *args, what: str, **kwargs):
    attempts = _Attempts(what)
    transient = 0
    lim = limiter()
    while True:
        lim.acquire_sync()
        token = _inside.set(True)
        try:
            return _sync_within_deadline(fn, args, kwargs, attempt_seconds(kwargs), what)
        except Exception as exc:
            error = exc
        finally:
            _inside.reset(token)
            lim.release()
        if is_transient(error) and transient < TRANSIENT_ATTEMPTS - 1:
            wait = TRANSIENT_WAITS[min(transient, len(TRANSIENT_WAITS) - 1)]
            transient += 1
            logger.warning("[llm] %s failed (attempt %d/%d), retrying in %.0fs: %s: %s",
                           what, transient, TRANSIENT_ATTEMPTS, wait,
                           type(error).__name__, str(error)[:200])
        else:
            wait = attempts.next_wait(error)
        if wait is None:
            raised = attempts.give_up(error)
            if raised is error:
                raise error
            raise raised from error
        time.sleep(wait)


def resilient_sync(fn):
    """Wrap a sync client method (vision_sync)."""
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        return call_sync(fn, self, *args, what=f"{type(self).__name__}.{fn.__name__}", **kwargs)
    wrapper.__resilient_sync__ = True
    return wrapper


def resilient_stream(fn):
    """Wrap an async-generator client method (stream)."""
    @functools.wraps(fn)
    async def wrapper(self, *args, **kwargs):
        async for piece in stream_async(fn, self, *args, what=f"{type(self).__name__}.{fn.__name__}", **kwargs):
            yield piece
    return wrapper
