"""Reversible app-side encryption for secrets that must be *recovered* at
point of use (currently only the per-space LLM connector ``auth_token``).

Design (see docs/architecture/SECURITY_ARCHITECTURE.md §6):
  * Fernet / MultiFernet symmetric encryption.
  * The key-encryption key (KEK) is delivered out-of-band as ``CONNECTOR_KEK``
    (a Kubernetes Secret on-prem). It is a comma-separated list of Fernet keys;
    the **first** key encrypts, **all** keys can decrypt — this is the rotation
    window (encrypt with the new key, still read rows written with the old one).
  * **Feature-gated:** when ``CONNECTOR_KEK`` is unset the box is *disabled* —
    ``is_enabled()`` is False and callers fall back to the global ``PROXY_*``
    credentials. This keeps standalone / local / unconfigured environments
    behaving exactly as before per-space credentials existed.

Passwords and API tokens are NOT stored here — those stay one-way hashed. Only
the forwardable connector token needs reversible storage.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

ENV_KEK = "CONNECTOR_KEK"

# Cached MultiFernet per key-environment-variable name. The dict distinguishes
# "not built yet" from "built and found no key" by holding None for the latter.
# reset() clears it (used by tests).
_boxes: dict[str, object] = {}


def build_box(env_name: str):
    """Build a MultiFernet from ``env_name``, or None when unset/invalid.

    Public because more than one kind of secret needs this mechanism with a
    different key and a different policy for a missing one: a connector token
    degrades to the global credentials, a database password must not degrade to
    anything. The mechanism is shared; the policy belongs to the caller.
    """
    raw = os.environ.get(env_name, "").strip()
    if not raw:
        return None
    try:
        from cryptography.fernet import Fernet, MultiFernet
    except ImportError:  # pragma: no cover - cryptography is a hard dep where used
        logger.error("%s is set but the 'cryptography' package is missing", env_name)
        return None
    keys = [k.strip() for k in raw.split(",") if k.strip()]
    try:
        return MultiFernet([Fernet(k.encode()) for k in keys])
    except Exception:
        # Never log the key material itself.
        logger.error("%s is set but is not a valid Fernet key list", env_name)
        return None


def get_box(env_name: str):
    """Cached :func:`build_box`. None when that variable holds no usable key."""
    if env_name not in _boxes:
        _boxes[env_name] = build_box(env_name)
    return _boxes[env_name]


def _get():
    return get_box(ENV_KEK)


def reset() -> None:
    """Drop the cached boxes so the next call re-reads the environment (tests)."""
    _boxes.clear()


def is_enabled() -> bool:
    """True when a usable KEK is configured (encryption/decryption available)."""
    return _get() is not None


def kek_status() -> dict:
    """Is a KEK configured here, is it usable, and which one — for health checks.

    ``fingerprint`` is the first 12 hex digits of SHA-256 over the configured key list,
    as :func:`build_box` parses it (so spacing around the commas does not count):
    equal fingerprints on two services mean they hold the same keys, so a token one
    encrypts the other can decrypt. It reveals nothing usable about the key itself.
    """
    import hashlib

    raw = os.environ.get(ENV_KEK, "").strip()
    keys = [k.strip() for k in raw.split(",") if k.strip()]
    try:
        import cryptography  # noqa: F401
    except ImportError:
        # A service without the library cannot use a key at all, so it cannot
        # say whether one is valid: "not checked", not "invalid". The test stub
        # is such a service — it never calls a model.
        return {
            "configured": None,
            "valid": False,
            "keys": len(keys),
            "length": len(keys[0]) if keys else 0,
            "fingerprint": None,
            "reason": "not checked: this service has no cryptography library (it uses no connector token)",
        }
    return {
        "configured": bool(raw),
        "valid": _get() is not None,
        "keys": len(keys),
        "length": len(keys[0]) if keys else 0,   # a Fernet key is 44 characters
        "fingerprint": hashlib.sha256(",".join(keys).encode()).hexdigest()[:12] if keys else None,
    }


def looks_sealed(token: Optional[str]) -> bool:
    """True when ``token`` has the shape of what :func:`encrypt` produces (a Fernet token).

    Lets a service tell a sealed token from a plaintext one without holding the key —
    without a KEK, the resolver otherwise takes the ciphertext for the token itself.
    """
    import base64
    import binascii

    if not token:
        return False
    try:
        data = base64.urlsafe_b64decode(token.encode())
    except (binascii.Error, ValueError):
        return False
    # version 0x80 | 8-byte timestamp | 16-byte IV | AES blocks (16 bytes each) | 32-byte HMAC
    return data[:1] == b"\x80" and len(data) >= 73 and (len(data) - 57) % 16 == 0


def encrypt(plaintext: str) -> str:
    """Encrypt a secret to a URL-safe token. Raises if the box is disabled."""
    box = _get()
    if box is None:
        raise RuntimeError(
            f"{ENV_KEK} is not configured; cannot encrypt connector secrets"
        )
    return box.encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    """Decrypt a token produced by :func:`encrypt`. Raises on disabled/invalid."""
    box = _get()
    if box is None:
        raise RuntimeError(
            f"{ENV_KEK} is not configured; cannot decrypt connector secrets"
        )
    return box.decrypt(token.encode()).decode()


def try_decrypt(token: Optional[str]) -> Optional[str]:
    """Best-effort decrypt for graceful fallback paths.

    Returns None (never raises) when the box is disabled, the token is empty,
    or decryption fails — so a caller can fall back to global credentials.
    """
    if not token or not is_enabled():
        return None
    try:
        return decrypt(token)
    except Exception:
        logger.warning("connector token failed to decrypt; falling back")
        return None


def generate_key() -> str:
    """Generate a fresh Fernet key (for provisioning the KEK / tests)."""
    from cryptography.fernet import Fernet

    return Fernet.generate_key().decode()
