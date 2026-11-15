"""`GET /health/telemetry` — is this pod actually exporting spans?

Separate from the liveness/readiness probes on purpose. Telemetry being off is
never a reason to restart a pod or pull it out of a Service, so this reports
rather than gates, and the probe payloads stay unchanged.

Registered here rather than inline at each call site so the response shape is
defined once: the templates get it via ``wire_http_observability``, the gateway
and agent register it themselves (they init telemetry directly).
"""

from __future__ import annotations

from shared.utils.telemetry.state import telemetry_state


def register_telemetry_health(app) -> None:
    """Add ``GET /health/telemetry`` to a FastAPI app."""

    @app.get("/health/telemetry", tags=["health"])
    async def health_telemetry():  # noqa: D401 - route handler
        """Effective tracing state: what is exporting, where, and how much."""
        return telemetry_state()
