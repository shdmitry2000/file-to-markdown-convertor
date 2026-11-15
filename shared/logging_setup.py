"""One log setup for every Python pod: one JSON object per line on stdout.

Splunk reads the stdout of every pod and alerts on the ``event`` field
(``shared.log_events``), so every pod must write the same, parseable line:

- JSON by default (``LOG_FORMAT=text`` for local reading), level from ``LOG_LEVEL``.
- A traceback stays inside the line (the ``exception`` field), never on lines of its own.
- uvicorn's own loggers are routed through the same handler, so its startup lines and
  its "Exception in ASGI application" tracebacks are JSON too.
- The line is built on the calling thread (the request's context vars — correlation
  id, request context — are only visible there) and only the stdout write happens on
  a background thread, so a slow log pipe never stalls a request.

Lives in ``shared/`` (not ``services/``) because the templates ship ``shared/`` only.
"""

from __future__ import annotations

import atexit
import json
import logging
import logging.handlers
import os
import queue
import sys
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from shared.utils.telemetry.correlation import get_correlation_id

request_context_var: ContextVar[Dict[str, Any]] = ContextVar("request_context", default={})

_UVICORN = ("uvicorn", "uvicorn.error", "uvicorn.access")
# LiteLLM logs whole prompts and raw embedding vectors at DEBUG.
_NOISY = ("httpx", "httpcore", "LiteLLM", "LiteLLM Router", "LiteLLM Proxy")

_listener: Optional[logging.handlers.QueueListener] = None


def set_request_context(**kwargs) -> None:
    """Set additional context for current request."""
    ctx = dict(request_context_var.get() or {})
    ctx.update(kwargs)
    request_context_var.set(ctx)


def get_request_context() -> Dict[str, Any]:
    """Get current request context."""
    return request_context_var.get() or {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class JSONFormatter(logging.Formatter):
    """One JSON object per record; exception text and structured fields inside it."""

    def __init__(self, service_name: str):
        super().__init__()
        self.service_name = service_name

    def format(self, record: logging.LogRecord) -> str:
        log_data = {
            "timestamp": _now(),
            "service": self.service_name,
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "correlation_id": get_correlation_id(),
        }
        ctx = get_request_context()
        if ctx:
            log_data["context"] = ctx
        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)
        if hasattr(record, "extra_fields"):
            log_data.update(record.extra_fields)
        return json.dumps(log_data, ensure_ascii=False, default=str)


class TextFormatter(logging.Formatter):
    """Human-readable line for local development (tracebacks stay multi-line here)."""

    def __init__(self, service_name: str):
        super().__init__()
        self.service_name = service_name

    def format(self, record: logging.LogRecord) -> str:
        line = (f"{_now()} [{self.service_name}] [{get_correlation_id()}] "
                f"{record.levelname:8s} {record.name}: {record.getMessage()}")
        ctx = get_request_context()
        if ctx:
            line += f" | context={ctx}"
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


class _FormattingQueueHandler(logging.handlers.QueueHandler):
    """Formats on the caller's thread (context vars live there), queues the text."""

    def prepare(self, record: logging.LogRecord) -> logging.LogRecord:
        out = logging.makeLogRecord({"msg": self.format(record), "levelno": record.levelno,
                                     "levelname": record.levelname, "name": record.name})
        return out


def _ours(handler: logging.Handler) -> bool:
    # Our previous handler, or a plain basicConfig StreamHandler. Subclasses (pytest's
    # capture handlers) are left alone.
    return getattr(handler, "_rag_logging", False) or type(handler) is logging.StreamHandler


def configure_logging(service_name: str, *, level: Optional[str] = None,
                      fmt: Optional[str] = None, stream: Any = None) -> None:
    """Install the one stdout handler on the root logger. Safe to call again."""
    global _listener
    level = (level or os.getenv("LOG_LEVEL") or "INFO").upper()
    fmt = (fmt or os.getenv("LOG_FORMAT") or "json").lower()
    service_name = os.getenv("SERVICE_NAME") or service_name

    root = logging.getLogger()
    root.setLevel(getattr(logging, level, logging.INFO))
    for handler in root.handlers[:]:
        if _ours(handler):
            root.removeHandler(handler)
    if _listener is not None:
        _listener.stop()
        _listener = None

    formatter = JSONFormatter(service_name) if fmt == "json" else TextFormatter(service_name)
    sink = logging.StreamHandler(stream or sys.stdout)
    sink.setFormatter(logging.Formatter("%(message)s"))
    q: queue.SimpleQueue = queue.SimpleQueue()
    handler = _FormattingQueueHandler(q)
    handler.setFormatter(formatter)
    handler._rag_logging = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    _listener = logging.handlers.QueueListener(q, sink)
    _listener.start()

    for name in _UVICORN:
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True
    # The gateway's audit line is the request record; per-request access lines
    # (health probes included) are noise.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    for name in _NOISY:
        logging.getLogger(name).setLevel(logging.WARNING)


def flush_logging() -> None:
    """Drain the queue (tests, shutdown)."""
    global _listener
    if _listener is not None:
        _listener.stop()
        _listener.start()


@atexit.register
def _stop_listener() -> None:
    if _listener is not None:
        _listener.stop()
