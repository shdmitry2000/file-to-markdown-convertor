"""Bound how long an ADC token refresh may block.

`google.auth` refreshes an OAuth token with a **120 second** connect timeout, and
nothing on our side chooses it: litellm calls ``credentials.refresh(Request())``
(vertex_llm_base.py) with no timeout, and
``google.auth.transport.requests.Request.__call__`` takes ``timeout=_DEFAULT_TIMEOUT``
— a *default argument*, so it binds at import and reassigning the module constant
afterwards has no effect.

Why it matters here. Egress to ``oauth2.googleapis.com`` from this dev VM is
intermittent. On 2026-09-07 an evaluation logged 19 refresh timeouts, and 11 of 30
questions scored zero retrieved chunks: no token, so no query embedding, so nothing
to search with. `search_error` stayed null the whole time because nothing threw at
the search layer — the aggregate just reported "11/30 questions failed search
(timeout/error)". Three of those calls also surfaced as gateway 503s
("Plugin service unavailable"), because a request blocked for two minutes outruns
the gateway's patience.

A blip should cost a retry, not a two-minute stall. The retry budget the platform
already sets (5 retries from 2 s, vertex_proxy.py, tuned for "faster
failures") cannot help when a *single* attempt blocks that long.

This is a monkeypatch, which is worth stating plainly: the timeout is
unreachable through configuration, and the alternative — forking litellm's Vertex
auth path — is far worse. It only lowers an unspecified default and leaves an
explicit caller-supplied timeout alone.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

#: Seconds a token refresh may spend connecting. Tunable because the right value
#: is a property of the network, not of this code.
ENV_VAR = "ADC_REFRESH_TIMEOUT_SECONDS"
DEFAULT_TIMEOUT_SECONDS = 10.0


def _configured_timeout() -> float:
    raw = os.getenv(ENV_VAR, "").strip()
    if not raw:
        return DEFAULT_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        logger.warning("%s=%r is not a number — using %ss", ENV_VAR, raw,
                       DEFAULT_TIMEOUT_SECONDS)
        return DEFAULT_TIMEOUT_SECONDS
    if value <= 0:
        logger.warning("%s=%r must be positive — using %ss", ENV_VAR, raw,
                       DEFAULT_TIMEOUT_SECONDS)
        return DEFAULT_TIMEOUT_SECONDS
    return value


def install(timeout_seconds: float | None = None) -> bool:
    """Shorten the default token-refresh timeout. Idempotent.

    Returns True if the patch is in place (including when a previous call
    installed it), False when google-auth is absent — which is normal for an
    image that never talks to Vertex, and must not be an error.
    """
    try:
        from google.auth.transport import requests as ga_requests
    except ImportError:
        return False

    original = ga_requests.Request.__call__
    if getattr(original, "_ragp_adc_timeout_patched", False):
        return True

    limit = _configured_timeout() if timeout_seconds is None else timeout_seconds
    google_default = getattr(ga_requests, "_DEFAULT_TIMEOUT", 120)

    def __call__(self, url, method="GET", body=None, headers=None,
                 timeout=google_default, **kwargs):
        # Only override the value google-auth itself would have chosen. A caller
        # that asked for a specific timeout meant it; the ambiguous case is a
        # caller explicitly passing 120, which is indistinguishable from the
        # default and is treated as the default.
        effective = limit if timeout == google_default else timeout
        return original(self, url, method=method, body=body, headers=headers,
                        timeout=effective, **kwargs)

    __call__._ragp_adc_timeout_patched = True  # type: ignore[attr-defined]
    ga_requests.Request.__call__ = __call__  # type: ignore[method-assign]
    logger.info(
        "ADC token refresh connect timeout bounded to %ss (google-auth default %ss)",
        limit, google_default,
    )
    return True
