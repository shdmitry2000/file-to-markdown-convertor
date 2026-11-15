"""Shared OpenTelemetry helpers.

Public API:
    init_telemetry(service_name)   — set up a tracer (no-op if endpoint/pkgs absent)
    instrument_app(app)            — auto-instrument FastAPI + httpx (no-op fallback)
    telemetry_state()              — effective tracing state (for /health/telemetry)
    register_telemetry_health(app) — expose that state on a FastAPI app
    get_current_trace_id()         — 32-hex trace id from active span, or uuid4 fallback
    get_correlation_id()           — canonical per-request id (adopted, else trace id)
    set_correlation_id(cid)        — adopt an inbound id for this request

Designed for closed/air-gapped environments: each helper degrades gracefully
when OTel packages are missing or no OTLP endpoint is configured. K8s flavor
(GKE / k3s / etc.) is irrelevant — transport is plain OTLP/gRPC.
"""

from shared.utils.telemetry.tracer import init_telemetry, TelemetryConfigError
from shared.utils.telemetry.instrumentation import instrument_app
from shared.utils.telemetry.state import telemetry_state
from shared.utils.telemetry.health import register_telemetry_health
from shared.utils.telemetry.trace_id import get_current_trace_id
from shared.utils.telemetry.correlation import (
    correlation_id_var,
    get_correlation_id,
    set_correlation_id,
)

__all__ = [
    "init_telemetry",
    "TelemetryConfigError",
    "instrument_app",
    "telemetry_state",
    "register_telemetry_health",
    "get_current_trace_id",
    "correlation_id_var",
    "get_correlation_id",
    "set_correlation_id",
]
