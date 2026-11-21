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

Where spans go is chosen by TRACING_BACKEND, a comma-separated list:

  TRACING_BACKEND             — any of "otel", "dynatrace", "log" (e.g.
                                "dynatrace,log"), or "none" alone. Unset = the old
                                rule: "otel" when OTEL_EXPORTER_OTLP_ENDPOINT is
                                set, else "none". An unknown name, "none" in a list,
                                or "otel" / "dynatrace" without its endpoint raises
                                TelemetryConfigError: the pod asked to export and can't.
  DYNATRACE_OTLP_ENDPOINT     — "dynatrace" only: the Dynatrace collector
                                (http://<svc>:4317 for gRPC) or the Dynatrace OTLP
                                API base (https://<host>/e/<env>/api/v2/otlp, http only).
  DYNATRACE_OTLP_PROTOCOL     — "grpc" (default) or "http/protobuf".
  DYNATRACE_API_TOKEN         — optional; sent as "Authorization: Api-Token <t>".
                                Secret. Needed by the Dynatrace API, not by a collector.

"log" sends nothing over the network: each finished request becomes one JSON line
on stdout (event "trace.request", category "trace") with its route, status,
duration and the time spent per inner step — see :class:`_RequestLogProcessor`.
Those lines reach Splunk through the same log forwarder as the security events,
which are written by shared.log_events and do not depend on tracing at all.

For "otel", reads OTEL_EXPORTER_OTLP_ENDPOINT to decide *whether* to export; everything
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
import threading
from collections import OrderedDict

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


_BACKENDS = ("otel", "dynatrace", "log")


def _resolve_backends() -> list[str]:
    """The chosen backends from TRACING_BACKEND, in order. Raises on contradictions."""
    raw = os.environ.get("TRACING_BACKEND", "").strip().lower()
    if not raw:
        return ["otel"] if os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip() else []
    names = list(dict.fromkeys(n.strip() for n in raw.split(",") if n.strip()))
    if names == ["none"]:
        return []
    bad = [n for n in names if n not in _BACKENDS]
    if bad:
        raise TelemetryConfigError(
            f"TRACING_BACKEND={raw!r}: {', '.join(bad)} not allowed. Use 'none' alone, "
            f"or a comma-separated list of {', '.join(_BACKENDS)}."
        )
    for name in names:
        if name != "log" and not _otlp_settings(name)[0]:
            required = "OTEL_EXPORTER_OTLP_ENDPOINT" if name == "otel" else "DYNATRACE_OTLP_ENDPOINT"
            raise TelemetryConfigError(
                f"TRACING_BACKEND includes {name} but {required} is empty, "
                f"so no span can be exported."
            )
    return names


def _otlp_settings(backend: str) -> tuple[str, str]:
    """(endpoint, protocol) of an OTLP backend."""
    prefix = "OTEL_EXPORTER_OTLP" if backend == "otel" else "DYNATRACE_OTLP"
    endpoint = os.environ.get(f"{prefix}_ENDPOINT", "").strip()
    protocol = os.environ.get(f"{prefix}_PROTOCOL", "grpc").strip().lower() or "grpc"
    return endpoint, protocol


def _dynatrace_exporter(exporter_cls, endpoint: str, protocol: str):
    """The OTLP exporter aimed at Dynatrace, built from DYNATRACE_* explicitly.

    The ``otel`` path leaves everything to the SDK's OTEL_* env; Dynatrace's
    settings have their own names, so they are passed as arguments here.
    """
    token = os.environ.get("DYNATRACE_API_TOKEN", "").strip()
    if protocol.startswith("http"):
        # An explicit endpoint is used verbatim by the HTTP exporter (only the
        # env-var form gets /v1/traces appended), so append it here.
        if not endpoint.rstrip("/").endswith("/v1/traces"):
            endpoint = endpoint.rstrip("/") + "/v1/traces"
        headers = {"Authorization": f"Api-Token {token}"} if token else None
        return exporter_cls(endpoint=endpoint, headers=headers)
    # gRPC metadata keys must be lowercase.
    headers = (("authorization", f"Api-Token {token}"),) if token else None
    return exporter_cls(
        endpoint=endpoint, headers=headers, insecure=endpoint.startswith("http://"),
    )


def _otlp_processor(backend: str, service_name: str):
    """BatchSpanProcessor exporting to an OTLP backend ("otel" or "dynatrace")."""
    endpoint, protocol = _otlp_settings(backend)
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
            f"TRACING_BACKEND includes {backend} ({endpoint!r}) but the "
            f"otlp-{protocol} exporter package is not installed, so no span can "
            f"ever be exported (service={service_name})."
        ) from exc

    if backend == "dynatrace":
        inner = _dynatrace_exporter(OTLPSpanExporter, endpoint, protocol)
    else:
        # Constructed with no arguments on purpose. The SDK then reads
        # OTEL_EXPORTER_OTLP_ENDPOINT/HEADERS/CERTIFICATE/TIMEOUT itself and
        # derives TLS from the URL scheme. Passing endpoint= and
        # insecure=True explicitly, as this did before, pinned every
        # deployment to a plaintext in-cluster collector and silently
        # dropped spans against any TLS or token-authenticated backend.
        inner = OTLPSpanExporter()
    exporter = _CountingSpanExporter(inner, SpanExportResult.SUCCESS)
    return BatchSpanProcessor(exporter), endpoint, protocol


_request_logger = logging.getLogger("telemetry.requests")


class _RequestLogProcessor:
    """The "log" backend: one JSON line per finished request, nothing per span.

    Child spans only add their duration to a per-trace tally (by span name); when
    the request's local root span ends — no parent, or a parent from another
    service — the tally is written with the root as ``event: trace.request``.
    The line carries the trace id, so it joins the audit log and, with "otel" or
    "dynatrace" also on, the full trace.

    Bounded: at most _MAX_OPEN unfinished traces are held (oldest dropped — a root
    that never ends here, e.g. a span continued in another process) and at most
    _MAX_STEPS step names per line (the slowest).
    """

    _MAX_OPEN = 2000
    _MAX_STEPS = 20

    def __init__(self, service_name: str):
        self._service = service_name
        self._open: "OrderedDict[int, dict]" = OrderedDict()
        self._lock = threading.Lock()

    def on_start(self, span, parent_context=None):
        pass

    def _on_ending(self, span):
        pass

    def on_end(self, span):
        try:
            self._on_end(span)
        except Exception:  # noqa: BLE001 — tracing must never break a request
            logger.debug("[telemetry] request log line failed", exc_info=True)

    def _on_end(self, span):
        trace_id = span.context.trace_id
        ms = (span.end_time - span.start_time) / 1e6
        attrs = span.attributes or {}
        parent = span.parent
        if parent is not None and not parent.is_remote:
            if "asgi.event.type" in attrs:
                return  # the ASGI send/receive sub-spans: noise, not steps
            with self._lock:
                steps = self._open.pop(trace_id, None) or {}
                self._open[trace_id] = steps
                while len(self._open) > self._MAX_OPEN:
                    self._open.popitem(last=False)
                tally = steps.setdefault(span.name, [0, 0.0])
                tally[0] += 1
                tally[1] += ms
            return

        with self._lock:
            steps = self._open.pop(trace_id, None) or {}
        status = attrs.get("http.response.status_code", attrs.get("http.status_code"))
        failed = span.status.status_code.name == "ERROR" or (
            isinstance(status, int) and status >= 500
        )
        top = sorted(steps.items(), key=lambda kv: kv[1][1], reverse=True)[: self._MAX_STEPS]
        line = {
            "event": "trace.request",
            "category": "trace",
            "outcome": "failure" if failed else "success",
            "trace_id": format(trace_id, "032x"),
            "span": span.name,
            "method": attrs.get("http.request.method", attrs.get("http.method")),
            "route": attrs.get("http.route"),
            "status": status,
            "duration_ms": round(ms, 1),
            "steps": [{"name": n, "count": c, "ms": round(t, 1)} for n, (c, t) in top],
        }
        line = {k: v for k, v in line.items() if v is not None}
        _request_logger.log(
            logging.WARNING if failed else logging.INFO,
            "trace.request %s %s %s %.0fms",
            line.get("method", ""), line.get("route", span.name), status or "", ms,
            extra={"extra_fields": line},
        )

    def shutdown(self):
        pass

    def force_flush(self, timeout_millis: int = 30000):
        return True


def init_telemetry(service_name: str):
    backends = _resolve_backends()
    label = ",".join(backends) or "none"

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
        endpoints = [_otlp_settings(b)[0] for b in backends if b != "log"]
        state.record_tracing(
            "sdk_missing", service_name=service_name,
            endpoint=", ".join(endpoints), backend=label,
        )
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

    endpoints: list[str] = []
    protocols: list[str] = []
    for backend in backends:
        if backend == "log":
            provider.add_span_processor(_RequestLogProcessor(effective_name))
            endpoint, protocol = "stdout", "log"
        else:
            processor, endpoint, protocol = _otlp_processor(backend, service_name)
            provider.add_span_processor(processor)
        endpoints.append(endpoint)
        protocols.append(protocol)
        logger.info(
            "[telemetry] %s backend: %s on %s (service=%s)",
            backend, protocol, endpoint, service_name,
        )

    if backends:
        state.record_tracing(
            "exporting", service_name=effective_name, endpoint=", ".join(endpoints),
            protocol=", ".join(protocols), backend=label,
        )
    else:
        logger.info(
            "[telemetry] TRACING_BACKEND=none — no exporter attached "
            "(service=%s); tracing is local-only",
            service_name,
        )
        state.record_tracing("disabled_no_endpoint", service_name=effective_name, backend=label)

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
