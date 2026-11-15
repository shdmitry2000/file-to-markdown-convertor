"""Effective telemetry state — what tracing is *actually* doing right now.

``init_telemetry`` and ``instrument_app`` degrade silently by design: missing OTel
packages, an unset endpoint, or an unreachable collector all leave the service
running. That is correct for an air-gapped deployment, but it left no way to tell
"we deliberately turned tracing off" apart from "tracing is broken" — the only
signal was a log line, and the unset-endpoint branch logged at DEBUG.

What this records is the *effective* state, not the requested config. That
distinction is the whole point: ``OTEL_SERVICE_NAME`` can be set on a pod whose
``OTEL_EXPORTER_OTLP_ENDPOINT`` is empty, so an env-var presence check reports
telemetry as configured while no span ever leaves the process.

The span counters cover the one failure no startup check can reach: a collector
that is configured, resolvable, and dead. ``spans_dropped`` climbing with
``spans_exported`` flat is that, and nothing else.
"""

from __future__ import annotations

import threading
from typing import Literal, Optional

# "uninitialized" is a real state, not a placeholder: it means init_telemetry never
# ran — a wiring bug, distinct from tracing being off on purpose.
Tracing = Literal[
    "uninitialized",         # init_telemetry was never called
    "exporting",             # endpoint set, exporter attached
    "disabled_no_endpoint",  # deliberate: OTEL_EXPORTER_OTLP_ENDPOINT unset
    "sdk_missing",           # opentelemetry-sdk not installed (expected air-gapped)
]

_lock = threading.Lock()

_tracing: Tracing = "uninitialized"
_service_name: Optional[str] = None
_endpoint: Optional[str] = None
_protocol: Optional[str] = None
_instrumented: list[str] = []
_spans_exported = 0
_spans_dropped = 0


def record_tracing(
    tracing: Tracing,
    *,
    service_name: Optional[str] = None,
    endpoint: Optional[str] = None,
    protocol: Optional[str] = None,
) -> None:
    """Record the outcome of ``init_telemetry``."""
    global _tracing, _service_name, _endpoint, _protocol
    with _lock:
        _tracing = tracing
        _service_name = service_name
        _endpoint = endpoint or None
        _protocol = protocol


def record_instrumented(names: list[str]) -> None:
    """Record which auto-instrumentations actually attached."""
    global _instrumented
    with _lock:
        _instrumented = list(names)


def count_spans(*, exported: int = 0, dropped: int = 0) -> None:
    """Tally an export batch. Called from the BatchSpanProcessor's worker thread."""
    global _spans_exported, _spans_dropped
    with _lock:
        _spans_exported += exported
        _spans_dropped += dropped


def reset() -> None:
    """Drop all recorded state (tests)."""
    global _tracing, _service_name, _endpoint, _protocol, _instrumented
    global _spans_exported, _spans_dropped
    with _lock:
        _tracing = "uninitialized"
        _service_name = _endpoint = _protocol = None
        _instrumented = []
        _spans_exported = _spans_dropped = 0


def telemetry_state() -> dict:
    """The effective telemetry state, safe to expose on a health endpoint.

    Contains no secret: the endpoint is an address, and OTLP credentials live in
    ``OTEL_EXPORTER_OTLP_HEADERS``, which is never read here.
    """
    with _lock:
        return {
            "tracing": _tracing,
            "service_name": _service_name,
            "endpoint": _endpoint,
            "protocol": _protocol,
            "instrumented": list(_instrumented),
            "spans_exported": _spans_exported,
            "spans_dropped": _spans_dropped,
        }
