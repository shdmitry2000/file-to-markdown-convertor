"""Canonical per-request correlation id.

One id per request, shared by every tier: log lines, the ``X-Correlation-ID``
response header, `audit.replay_payloads.correlation_id`, and the OTel trace.
Because it is a single 32-hex value, a Splunk log line, a stored replay payload
and a trace all join on the same key.

Resolution order:

  1. A value explicitly adopted for this request via `set_correlation_id()` —
     the gateway's audit middleware sets the inbound ``x-correlation-id`` here so
     a caller-supplied id wins end-to-end.
  2. Otherwise `get_current_trace_id()` — the active OTel trace id, or a uuid4
     fallback when OTel is absent.

The fallback is memoized into the contextvar on first read: `get_current_trace_id()`
mints a *fresh* uuid every call when no OTel span is active, so without memoizing,
two log lines in one request would carry different ids.

Lives in `shared/utils/` (not `services/`) so standalone templates get the same
id semantics as the service tier without importing the service package.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Optional

from shared.utils.telemetry.trace_id import get_current_trace_id

correlation_id_var: ContextVar[Optional[str]] = ContextVar(
    "correlation_id", default=None
)


def get_correlation_id() -> str:
    """The id for the current request, minting and memoizing one if unset."""
    correlation_id = correlation_id_var.get()
    if correlation_id is None:
        correlation_id = get_current_trace_id()
        correlation_id_var.set(correlation_id)
    return correlation_id


def set_correlation_id(correlation_id: str) -> None:
    """Adopt an explicit id for the current request (e.g. an inbound header)."""
    correlation_id_var.set(correlation_id)
