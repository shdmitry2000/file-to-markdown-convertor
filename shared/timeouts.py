"""The basic timeouts: the only timeout numbers the product should have.

Everything that bounds a wait is built from these (plan: ~/.claude/plans/timeouts-plan.md):

- one unit each of the things a request can wait on (an LLM call, an embedding call,
  a DB statement, a plain call to another service, a health probe);
- the delta a caller adds over the service it calls, so a caller never gives up
  before its callee;
- the keepalive interval of a stream (a stream is idle-dead after three missed frames).

A template's search and answer budgets, and the waits of the gateway and the UI, are
computed from these. The defaults here are the defaults; deployments set the values in
the shared Helm env block. The frontend mirrors this table in
``frontend/src/lib/timeouts.ts``; ``tests/shared/test_timeouts_guard.py`` keeps the two
equal.

Values are read from the environment at call time, so a test or a settings reload
sees the current value.
"""

from __future__ import annotations

import logging
import os

from shared.rag_utils.env import env_seconds

logger = logging.getLogger(__name__)

#: name -> default seconds. The single list of basic timeouts.
BASIC_TIMEOUTS: dict[str, float] = {
    # One LLM call made while a user waits (search, answer), busy-provider wait included.
    "LLM_QUERY_TIMEOUT_SECONDS": 120.0,
    # One LLM attempt in background work (ingest, compile, judge); llm_factory adds its
    # busy-provider wait on top. Same name and default as shared/llm_factory/core/generation.py.
    "LLM_REQUEST_TIMEOUT_SECONDS": 300.0,
    # One embedding call (one batch).
    "EMBEDDING_TIMEOUT_SECONDS": 30.0,
    # One DB statement, or one wait for a pooled connection.
    "DB_TIMEOUT_SECONDS": 15.0,
    # One plain call between services (settings, files, run start/status, IdP).
    "HTTP_TIMEOUT_SECONDS": 30.0,
    # One health / readiness probe.
    "PROBE_TIMEOUT_SECONDS": 5.0,
    # What every caller adds over the budget of the service it calls.
    "HOP_DELTA_SECONDS": 10.0,
    # A keepalive frame on every stream; a stream with three missed frames is dead.
    "STREAM_KEEPALIVE_SECONDS": 15.0,
}

#: Missed keepalive frames after which a stream counts as dead.
STREAM_MISSED_FRAMES = 3


def seconds(name: str) -> float:
    """The current value of one basic timeout (env, else its default)."""
    return env_seconds(name, BASIC_TIMEOUTS[name])


def llm_query() -> float:
    return seconds("LLM_QUERY_TIMEOUT_SECONDS")


def llm_request() -> float:
    return seconds("LLM_REQUEST_TIMEOUT_SECONDS")


def embedding() -> float:
    return seconds("EMBEDDING_TIMEOUT_SECONDS")


def db() -> float:
    return seconds("DB_TIMEOUT_SECONDS")


def http() -> float:
    return seconds("HTTP_TIMEOUT_SECONDS")


def probe() -> float:
    return seconds("PROBE_TIMEOUT_SECONDS")


def hop_delta() -> float:
    return seconds("HOP_DELTA_SECONDS")


def stream_keepalive() -> float:
    return seconds("STREAM_KEEPALIVE_SECONDS")


def stream_idle() -> float:
    """How long a stream may stay silent before it counts as dead."""
    return stream_keepalive() * STREAM_MISSED_FRAMES


def shutdown_drain() -> float:
    """How long a stopping pod drains in-flight requests (UVICORN_TIMEOUT_GRACEFUL_SHUTDOWN
    in the lane values): one query LLM call, one plain call and a hop delta — enough
    for the requests measured to finish, short enough for rollouts. Ingest runs cut
    off by a stop are resumed by the sweeper; the Kubernetes grace period adds one
    more delta so uvicorn ends its drain before the kill."""
    return llm_query() + http() + hop_delta()


def caller_wait(callee_seconds: float, hops: int = 1) -> float:
    """What a caller waits for a callee whose own budget is ``callee_seconds``.

    ``hops`` is the number of hops between them (the UI calling a template through
    the gateway is two). A caller never waits less than what it calls.
    """
    return callee_seconds + hops * hop_delta()


# ── A RAG that reports no limit of its own ────────────────────────────────────
# Every template reports its Timeouts (GET /settings/timeouts); this is the wait for
# one that cannot be asked (unreachable, or an older image mid-rollout): the worst
# computed default of the shared-core templates — vector-v2's self_reflective path,
# 2 LLM + 3 embedding + 6 database calls; an answer adds one LLM call and diagnostics
# (2 database reads, 1 probe). Priced with the basics above, so it follows them.
# RAG_SEARCH_TIMEOUT_SECONDS / RAG_ANSWER_TIMEOUT_SECONDS no longer exist.

def unknown_rag_wait(kind: str) -> float:
    """What a caller allows a RAG that reported no limit, for a search or an answer."""
    search = 2 * llm_query() + 3 * embedding() + 6 * db()
    return search if kind == "search" else search + llm_query() + 2 * db() + probe()


def provisional_search_budget() -> float:
    """A template without declared step costs: what its search may take."""
    return unknown_rag_wait("search")


def provisional_answer_budget() -> float:
    return unknown_rag_wait("answer")


def timed_out_message(what: str, seconds: float) -> str:
    """What a user reads when a search or an answer ran out of its time limit.

    ``what`` is "search" or "answer". Says what timed out, after how long, and what
    to do; the step that was running is in the template's log, not here.
    """
    return (f"The {what} timed out after {seconds:.0f} s (this RAG's {what} timeout). "
            f"Try again, or raise the timeout in this RAG's settings.")


#: Timeout settings that no longer exist, and what replaced each. A deployment that still
#: sets one is told once per process at start; nothing reads them.
REMOVED_SETTINGS: dict[str, str] = {
    "RAG_SEARCH_TIMEOUT_SECONDS": "each RAG's Timeouts setting (its Parameters tab)",
    "RAG_ANSWER_TIMEOUT_SECONDS": "each RAG's Timeouts setting (its Parameters tab)",
    "TEMPLATE_REQUEST_TIMEOUT_SECONDS": "HTTP_TIMEOUT_SECONDS",
    "TEMPLATE_STATUS_TIMEOUT_SECONDS": "HTTP_TIMEOUT_SECONDS",
    "TEMPLATE_PROBE_TIMEOUT_SECONDS": "PROBE_TIMEOUT_SECONDS",
    "CONFIG_REPORT_FANOUT_TIMEOUT_SECONDS": "PROBE_TIMEOUT_SECONDS",
    "INGEST_STREAM_KEEPALIVE_SECONDS": "STREAM_KEEPALIVE_SECONDS",
    "INGEST_RUN_STREAM_KEEPALIVE_SECONDS": "STREAM_KEEPALIVE_SECONDS",
    "INGEST_REQUEST_TIMEOUT_SECONDS": "nothing (production ingest is a run)",
    "RAGP_POOL_TIMEOUT": "DB_TIMEOUT_SECONDS",
    "RAGP_STATEMENT_TIMEOUT": "DB_TIMEOUT_SECONDS",
    "PG_POOL_TIMEOUT": "DB_TIMEOUT_SECONDS",
    "PG_STATEMENT_TIMEOUT": "DB_TIMEOUT_SECONDS",
}


def warn_removed_settings() -> list[str]:
    """Log one WARNING per removed timeout setting still set here. Returns their names."""
    found = [name for name in REMOVED_SETTINGS if os.environ.get(name)]
    for name in found:
        logger.warning("%s is set but no longer read; use %s instead (timeouts plan).",
                       name, REMOVED_SETTINGS[name])
    return found


warn_removed_settings()  # once per process, when anything first uses the timeouts
