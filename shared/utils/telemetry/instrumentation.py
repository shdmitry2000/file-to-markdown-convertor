"""Auto-instrumentation for FastAPI + httpx.

`instrument_app(app)` enables:
  - FastAPI: every HTTP request creates a span; `traceparent` header is read
    on inbound calls (W3C propagation).
  - httpx: outbound calls inject `traceparent` so downstream services see the
    same trace_id.

Each instrumentation is wrapped in its own try/except so a missing package
doesn't take the whole service down. Designed for air-gapped: if no
instrumentation packages are installed, this is a silent no-op.

Which ones actually attached is recorded in :mod:`shared.utils.telemetry.state`
and returned, because a partial attach is invisible otherwise: httpx missing
while FastAPI succeeds still produces spans, so tracing looks healthy right up
until you notice no trace crosses a service boundary.
"""

import logging

from shared.utils.telemetry import state

logger = logging.getLogger(__name__)


def instrument_app(app) -> list[str]:
    """Attach auto-instrumentation; returns the names that succeeded."""
    attached = []
    if _instrument_fastapi(app):
        attached.append("fastapi")
    if _instrument_httpx():
        attached.append("httpx")
    state.record_instrumented(attached)
    return attached


def _instrument_fastapi(app) -> bool:
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    except ImportError:
        logger.info("[telemetry] fastapi instrumentation pkg missing — skipping")
        return False
    try:
        FastAPIInstrumentor.instrument_app(app)
        logger.debug("[telemetry] FastAPI auto-instrumentation enabled")
        return True
    except Exception as e:
        logger.warning("[telemetry] FastAPI instrumentation failed: %s", e)
        return False


def _instrument_httpx() -> bool:
    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    except ImportError:
        logger.info("[telemetry] httpx instrumentation pkg missing — skipping")
        return False
    try:
        HTTPXClientInstrumentor().instrument()
        logger.debug("[telemetry] httpx auto-instrumentation enabled")
        return True
    except Exception as e:
        logger.warning("[telemetry] httpx instrumentation failed: %s", e)
        return False
