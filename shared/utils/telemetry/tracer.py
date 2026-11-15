"""Tracer setup.

`init_telemetry(service_name)` returns a Tracer. The failure modes are handled so
services come up regardless — except one genuine contradiction, below:

  1. OTel packages not installed       → return _NoOpTracer
  2. OTEL_EXPORTER_OTLP_ENDPOINT unset → tracer is local (no exporter)
  3. Collector unreachable             → BatchSpanProcessor buffers and drops

Every outcome is recorded in :mod:`shared.utils.telemetry.state` and served by
``/health/telemetry``, because a log line is not an answer to "is this pod
exporting spans right now?" — case 2 in particular used to log at DEBUG only.

The one hard failure is an endpoint that is set while the exporter package for
the chosen protocol is missing: the deployment explicitly asked to export and it
cannot, which is a config contradiction rather than an air-gap. That raises
:class:`TelemetryConfigError`. Case 1 — no OTel SDK at all — stays non-fatal even
with an endpoint set, since a minimal air-gapped image legitimately ships without
the packages while inheriting a cluster-wide ConfigMap; it reports ``sdk_missing``
with the requested endpoint attached, so the mismatch is visible without a boot loop.

Reads OTEL_EXPORTER_OTLP_ENDPOINT to decide *whether* to export; everything
about *how* is left to the OTel SDK, which reads the standard environment
itself. That is deliberate: the telemetry backend is not chosen yet, and this
way pointing at a different one is a config change with no code change.

  OTEL_EXPORTER_OTLP_ENDPOINT — collector address. Unset = no exporter.
                                http:// exports in plaintext, https:// over TLS.
  OTEL_EXPORTER_OTLP_PROTOCOL — "grpc" (default, port 4317) or "http/protobuf"
                                (port 4318). Managed backends usually want HTTP.
  OTEL_EXPORTER_OTLP_HEADERS  — e.g. "authorization=Bearer xyz". Required by
                                every hosted backend; keep it in the Secret.
  OTEL_SERVICE_NAME           — overrides the *service_name* argument. Set by the
                                chart, so the deployed value is the one that wins.
  OTEL_RESOURCE_ATTRIBUTES    — e.g. "deployment.environment=prod".
"""

import logging
import os

from shared.utils.telemetry import state

logger = logging.getLogger(__name__)


class TelemetryConfigError(RuntimeError):
    """An OTLP endpoint is configured but exporting it is impossible.

    Raised out of ``init_telemetry`` on purpose, and re-raised by the callers that
    otherwise swallow telemetry errors — this is the one telemetry problem that
    should stop a pod rather than degrade it.
    """


class _CountingSpanExporter:
    """Delegating exporter that tallies spans shipped vs dropped.

    A configured, resolvable, *dead* collector is the failure no startup check can
    reach: the SDK buffers, retries, and drops on its own worker thread without
    ever failing a request. Counting at the exporter is the only place that sees it.

    Duck-typed rather than a SpanExporter subclass so this module still imports
    with no OTel installed; BatchSpanProcessor only calls these three methods.
    """

    def __init__(self, inner, success):
        self._inner = inner
        self._success = success

    def export(self, spans):
        n = len(spans)
        try:
            result = self._inner.export(spans)
        except Exception:
            state.count_spans(dropped=n)
            raise
        if result is self._success:
            state.count_spans(exported=n)
        else:
            state.count_spans(dropped=n)
        return result

    def shutdown(self):
        return self._inner.shutdown()

    def force_flush(self, timeout_millis: int = 30000):
        return self._inner.force_flush(timeout_millis)


def init_telemetry(service_name: str):
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()

    try:
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.resources import Resource, SERVICE_NAME
    except ImportError:
        logger.warning(
            "[telemetry] opentelemetry SDK not installed — tracer is a no-op for '%s'",
            service_name,
        )
        # Keep the endpoint on the record: unset means "off on purpose", set means
        # the deployment wanted export from an image that cannot do it.
        state.record_tracing("sdk_missing", service_name=service_name, endpoint=endpoint)
        return _NoOpTracer()

    # Resource.create (not Resource(attributes=...)) so the SDK's env detector runs
    # and OTEL_RESOURCE_ATTRIBUTES is honored — the constructor form skips detection
    # entirely, which silently dropped every attribute the chart set. Attributes
    # passed here outrank the env, so SERVICE_NAME is only passed when
    # OTEL_SERVICE_NAME is absent; otherwise the chart's value would be overridden
    # by the code argument and the deployed setting would be dead config.
    env_service = os.environ.get("OTEL_SERVICE_NAME", "").strip()
    resource = Resource.create({} if env_service else {SERVICE_NAME: service_name})
    provider = TracerProvider(resource=resource)

    # Report the name spans actually carry, not the argument — otherwise the health
    # endpoint disagrees with the traces whenever the chart sets OTEL_SERVICE_NAME.
    effective_name = env_service or service_name

    protocol = os.environ.get("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc").strip().lower()
    if endpoint:
        try:
            if protocol.startswith("http"):
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                    OTLPSpanExporter,
                )
            else:
                from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                    OTLPSpanExporter,
                )
            from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExportResult
        except ImportError as exc:
            raise TelemetryConfigError(
                f"OTEL_EXPORTER_OTLP_ENDPOINT is set to {endpoint!r} but the "
                f"otlp-{protocol} exporter package is not installed, so no span can "
                f"ever be exported (service={service_name})."
            ) from exc

        # Constructed with no arguments on purpose. The SDK then reads
        # OTEL_EXPORTER_OTLP_ENDPOINT/HEADERS/CERTIFICATE/TIMEOUT itself and
        # derives TLS from the URL scheme. Passing endpoint= and
        # insecure=True explicitly, as this did before, pinned every
        # deployment to a plaintext in-cluster collector and silently
        # dropped spans against any TLS or token-authenticated backend.
        exporter = _CountingSpanExporter(OTLPSpanExporter(), SpanExportResult.SUCCESS)
        provider.add_span_processor(BatchSpanProcessor(exporter))
        logger.info(
            "[telemetry] OTLP %s exporter on %s (service=%s)",
            protocol, endpoint, service_name,
        )
        state.record_tracing(
            "exporting", service_name=effective_name, endpoint=endpoint, protocol=protocol,
        )
    else:
        logger.info(
            "[telemetry] OTEL_EXPORTER_OTLP_ENDPOINT unset — no exporter attached "
            "(service=%s); tracing is local-only",
            service_name,
        )
        state.record_tracing("disabled_no_endpoint", service_name=effective_name)

    trace.set_tracer_provider(provider)
    return trace.get_tracer(service_name)


class _NoOpSpan:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def set_attribute(self, *_):
        pass

    def record_exception(self, *_):
        pass

    def set_status(self, *_, **__):
        pass


class _NoOpTracer:
    def start_as_current_span(self, *_, **__):
        return _NoOpSpan()

    def start_span(self, *_, **__):
        return _NoOpSpan()
