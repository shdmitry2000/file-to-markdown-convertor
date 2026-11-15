"""Trace-id retrieval with always-on fallback.

`get_current_trace_id()` returns a 32-character hex string identifying the
current request. Priority:

  1. The trace_id of the active OTel span, if any.
  2. The trace_id parsed from the inbound `traceparent` header (set by the
     auto-instrumentation when a downstream service receives a propagated
     request).
  3. A fresh `uuid.uuid4().hex` — guarantees every request has a unique id
     even when OTel is absent or not yet entered.

This means the same 32-hex format flows through logs whether OTel is active
or not — Splunk queries by `correlation_id` field don't care which path
generated the id.
"""

import uuid


def get_current_trace_id() -> str:
    try:
        from opentelemetry import trace
    except ImportError:
        return uuid.uuid4().hex

    span = trace.get_current_span()
    ctx = span.get_span_context() if span is not None else None
    if ctx is not None and ctx.is_valid:
        # trace_id is an int; format as 32-char lowercase hex (OTel canonical form)
        return format(ctx.trace_id, "032x")
    return uuid.uuid4().hex
