"""AUTH_BACKEND policy and Bearer parsing shared by gateway and credential gates.

Single source of truth for which auth backend is active and when ingress
``X-Forwarded-*`` headers are trusted as identity.
"""

import os
from typing import Optional


def auth_backend_name() -> str:
    """Normalized ``AUTH_BACKEND`` value (default ``internal``)."""
    raw = os.getenv("AUTH_BACKEND", "internal").strip().lower()
    return raw or "internal"


def forwarded_identity_trusted() -> bool:
    """True when ingress SSO ``X-Forwarded-*`` headers may carry identity."""
    return auth_backend_name() in ("connector", "dual")


def ingress_strips_forwarded_headers() -> bool:
    """Operator attestation that the ingress strips client-supplied identity headers."""
    raw = os.getenv("INGRESS_STRIPS_FORWARDED_HEADERS", "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def assert_forwarded_headers_safe() -> None:
    """Refuse to start a forwarded-identity backend without an ingress-strip attestation.

    When ``AUTH_BACKEND`` is ``connector``/``dual`` the app trusts ``X-Forwarded-*``
    identity headers at face value. Unless the ingress is proven to strip
    client-supplied ``X-Forwarded-*``/``SPACE`` headers, a client could forge an
    identity or group set. This converts that deployment prerequisite into an
    enforced startup gate (see docs/architecture/SECURITY_ARCHITECTURE.md §7).
    """
    if forwarded_identity_trusted() and not ingress_strips_forwarded_headers():
        raise RuntimeError(
            f"AUTH_BACKEND={auth_backend_name()!r} trusts forwarded identity headers, but "
            "INGRESS_STRIPS_FORWARDED_HEADERS is not set. Refusing to start: a client could "
            "forge X-Forwarded-User/-Groups. Configure the ingress to strip client-supplied "
            "X-Forwarded-*/SPACE headers, then set INGRESS_STRIPS_FORWARDED_HEADERS=true to "
            "attest it. See docs/architecture/SECURITY_ARCHITECTURE.md §7."
        )


def bearer_from_authorization(authorization: Optional[str]) -> Optional[str]:
    """Extract the bare token from an ``Authorization: Bearer <token>`` header."""
    if not authorization:
        return None
    stripped = authorization.strip()
    if stripped.lower().startswith("bearer "):
        token = stripped.split(" ", 1)[1].strip()
        return token or None
    return None


def has_bearer_authorization(authorization: Optional[str]) -> bool:
    """True when *authorization* contains a non-empty Bearer token."""
    return bearer_from_authorization(authorization) is not None
