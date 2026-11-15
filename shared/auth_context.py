"""Request-scoped caller credential for outbound gateway calls.

The caller's Bearer token (the user's identity) is captured once per request by
a FastAPI middleware and read at the moment of each outbound gateway call by
``UnifiedRAGClient``. Reading per-call (rather than baking the token into a
long-lived HTTP client) keeps identity correct even when a single client is
reused across requests from different users — ContextVars are isolated per
async task, so concurrent requests never see each other's token.

Security: this is for the *user RAG* lane only. There is no environment-variable
fallback here — a missing token means the gateway will reject the call (401/403),
which is the intended fail-loud behaviour. The service/CLI lane passes its token
explicitly via ``UnifiedRAGClient(auth_token=...)`` instead.
"""

from contextvars import ContextVar
from typing import Optional

from starlette.requests import Request

from shared.auth_backend import bearer_from_authorization, forwarded_identity_trusted

_caller_bearer: ContextVar[Optional[str]] = ContextVar("caller_bearer", default=None)
_caller_forwarded: ContextVar[Optional[dict[str, str]]] = ContextVar(
    "caller_forwarded", default=None
)

# Who the caller is, as the gateway's /api/auth/me said while the session was valid:
# {"x-on-behalf-of": user_id, ...}. Set by the credential gate when
# INTERNAL_SERVICE_TOKEN is on; gateway calls then send it instead of the session,
# so work that outlives the browser session keeps the user's identity.
_caller_on_behalf: ContextVar[Optional[dict[str, str]]] = ContextVar(
    "caller_on_behalf", default=None
)

FORWARDED_IDENTITY_HEADERS: tuple[str, ...] = (
    "x-forwarded-user",
    "x-forwarded-roles",
    "x-forwarded-global-admin",
)


def set_caller_bearer(token: Optional[str]) -> object:
    """Set the caller's bare token (no 'Bearer ' prefix) for the current
    request/task. Returns a reset marker to pass to :func:`reset_caller_bearer`.
    """
    return _caller_bearer.set(token)


def reset_caller_bearer(marker: object) -> None:
    """Restore the previous value using the marker from :func:`set_caller_bearer`."""
    _caller_bearer.reset(marker)


def get_caller_bearer() -> Optional[str]:
    """Return the current caller's bare token, or None if unset."""
    return _caller_bearer.get()


def set_caller_on_behalf(headers: Optional[dict[str, str]]) -> object:
    """Set the on-behalf-of headers for the current request/task."""
    return _caller_on_behalf.set(headers)


def reset_caller_on_behalf(marker: object) -> None:
    _caller_on_behalf.reset(marker)


def get_caller_on_behalf() -> dict[str, str]:
    """The on-behalf-of headers for gateway calls, or {} when none were resolved."""
    value = _caller_on_behalf.get()
    return dict(value) if value else {}


def set_caller_forwarded(headers: Optional[dict[str, str]]) -> object:
    """Set ingress SSO forwarded identity headers for the current request/task."""
    return _caller_forwarded.set(headers)


def reset_caller_forwarded(marker: object) -> None:
    _caller_forwarded.reset(marker)


def get_caller_forwarded() -> dict[str, str]:
    """Forwarded identity headers for AUTH_BACKEND=connector hops."""
    value = _caller_forwarded.get()
    return dict(value) if value else {}


def forwarded_identity_from_request(request: Request) -> dict[str, str]:
    """Extract forwarded identity headers from an inbound request.

    Returns ``{}`` unless ``AUTH_BACKEND`` is ``connector``/``dual`` — in ``internal``
    mode the headers are untrusted (no ingress strips client-supplied ones), so they
    are never treated as identity. This is the single chokepoint: the credential gate,
    the context var, and downstream propagation all read through here.
    """
    if not forwarded_identity_trusted():
        return {}
    out: dict[str, str] = {}
    for name in FORWARDED_IDENTITY_HEADERS:
        raw = request.headers.get(name)
        if raw and raw.strip():
            out[name] = raw.strip()
    return out


def has_caller_identity(request: Request) -> bool:
    """True when the request carries Bearer/cookie or ingress SSO identity."""
    if caller_token_from_request(request) is not None:
        return True
    return bool(forwarded_identity_from_request(request).get("x-forwarded-user"))


def caller_token_from_request(request: Request) -> Optional[str]:
    """Bare token from ``Authorization: Bearer`` header or ``ragp_session`` cookie.

    Authorization wins. The cookie fallback supports EventSource SSE callers that
    cannot set custom headers.
    """
    token = bearer_from_authorization(request.headers.get("authorization"))
    if token is None:
        return request.cookies.get("ragp_session") or None
    return token


