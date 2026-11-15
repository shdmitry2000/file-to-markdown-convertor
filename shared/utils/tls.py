"""One TLS policy for every outbound HTTPS client.

Production sits behind a corporate CA (OpenShift, self-signed chain). httpx
trusts certifi only, so a client built with ``verify=True`` fails there with
``CERTIFICATE_VERIFY_FAILED`` unless that CA is handed to it.

``TLS_VERIFY`` picks the mode:

    auto    (default) verify against certifi + the mounted CA bundle when one
            exists; with no bundle, skip verification and warn once — a
            self-signed deployment works with no configuration.
    strict  always verify; with no bundle, certifi only.
    off     never verify.

The bundle is ``TLS_CA_BUNDLE`` (falling back to ``SSL_CERT_FILE``), used only
if the file exists. It is ADDED to certifi rather than replacing it, so public
endpoints keep verifying. ``SSL_VERIFY=false`` (the earlier knob) still means
``off`` when ``TLS_VERIFY`` is unset.
"""

from __future__ import annotations

import functools
import logging
import os
import ssl
from typing import Optional, Union

logger = logging.getLogger(__name__)

__all__ = ["httpx_verify", "install", "describe", "tls_mode", "ca_bundle_path", "check_all"]

ENV_TLS_VERIFY = "TLS_VERIFY"
ENV_TLS_CA_BUNDLE = "TLS_CA_BUNDLE"
ENV_SSL_VERIFY = "SSL_VERIFY"  # legacy boolean, see module docstring

_MODES = ("auto", "strict", "off")
_FALSEY = ("0", "false", "no", "off")


def tls_mode() -> str:
    raw = (os.environ.get(ENV_TLS_VERIFY) or "").strip().lower()
    if raw in _MODES:
        return raw
    if raw:
        logger.warning("%s=%r is not one of %s; using 'auto'", ENV_TLS_VERIFY, raw, _MODES)
        return "auto"
    if (os.environ.get(ENV_SSL_VERIFY) or "").strip().lower() in _FALSEY:
        return "off"
    return "auto"


def ca_bundle_path() -> Optional[str]:
    for env in (ENV_TLS_CA_BUNDLE, "SSL_CERT_FILE"):
        path = (os.environ.get(env) or "").strip()
        if path and os.path.isfile(path):
            return path
    return None


@functools.lru_cache(maxsize=1)
def _resolve() -> tuple[str, Optional[str], Union[ssl.SSLContext, bool]]:
    mode = tls_mode()
    bundle = ca_bundle_path()
    if mode == "off":
        logger.warning("TLS verification disabled (%s=off)", ENV_TLS_VERIFY)
        return mode, bundle, False
    if bundle:
        import certifi

        ctx = ssl.create_default_context(cafile=certifi.where())
        ctx.load_verify_locations(cafile=bundle)
        return mode, bundle, ctx
    if mode == "auto":
        logger.warning(
            "TLS verification disabled: no CA bundle "
            "(set %s, or %s=strict to require verification)",
            ENV_TLS_CA_BUNDLE, ENV_TLS_VERIFY,
        )
        return mode, None, False
    return mode, None, True


def httpx_verify() -> Union[ssl.SSLContext, bool]:
    """The ``verify=`` argument for an httpx client."""
    return _resolve()[2]


def describe() -> str:
    mode, bundle, verify = _resolve()
    return (
        f"tls: mode={mode} bundle={bundle or '-'} "
        f"verifying={'no' if verify is False else 'yes'}"
    )


def install() -> None:
    """Apply the policy process-wide, once, at service startup.

    Logs the one-line summary and hands the same ``verify`` to litellm, which
    builds its own httpx clients and accepts an SSLContext. Everything else
    passes ``httpx_verify()`` per client.
    """
    logger.info(describe())
    try:
        import litellm
    except ImportError:  # a service without the litellm lane
        return
    litellm.ssl_verify = httpx_verify()


def reset() -> None:
    """Forget the resolved policy (tests)."""
    _resolve.cache_clear()


# ── Certificate check (health page) ──────────────────────────────────────────
#
# For every https:// URL the service is configured with, say whether a client
# under this policy would accept the server's certificate, and if not, why —
# self-signed, unknown CA, hostname mismatch, expired. With verification off the
# handshake still runs against certifi + bundle, so the page shows what WOULD
# fail if verification were turned on, instead of an all-green lie.

# OpenSSL X509_V_ERR_* codes worth naming; anything else is "untrusted".
_VERIFY_CODES = {
    10: "expired",
    18: "self_signed",          # the server's own certificate is self-signed
    19: "self_signed_chain",    # a self-signed CA in the chain we do not trust
    20: "unknown_ca",           # issuer not in the trust store
    21: "unknown_ca",
    62: "hostname_mismatch",
}


def https_endpoints_from_env() -> dict[str, str]:
    """``{ENV_NAME: url}`` for every env var whose value is an ``https://`` URL."""
    return {
        k: v.strip() for k, v in sorted(os.environ.items())
        if (v or "").strip().lower().startswith("https://")
    }


def _check_context() -> ssl.SSLContext:
    verify = _resolve()[2]
    if isinstance(verify, ssl.SSLContext):
        return verify
    import certifi

    ctx = ssl.create_default_context(cafile=certifi.where())
    bundle = ca_bundle_path()
    if bundle:
        ctx.load_verify_locations(cafile=bundle)
    return ctx


def _cert_summary(der: bytes) -> dict:
    from cryptography import x509

    cert = x509.load_der_x509_certificate(der)
    return {
        "subject": cert.subject.rfc4514_string(),
        "issuer": cert.issuer.rfc4514_string(),
        "self_signed": cert.subject == cert.issuer,
        "not_after": cert.not_valid_after_utc.isoformat(),
    }


def check_endpoint(url: str, timeout: float = 5.0) -> dict:
    """TLS handshake to *url*'s host under the current policy. Blocking."""
    import socket
    from urllib.parse import urlsplit
    from urllib.request import proxy_bypass_environment

    parts = urlsplit(url)
    host, port = parts.hostname or "", parts.port or 443
    proxied = bool(os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")) \
        and not proxy_bypass_environment(host)
    result = {"host": host, "port": port, "via_proxy": proxied}

    def peer_cert() -> dict:
        insecure = ssl._create_unverified_context()  # only to read the cert we rejected
        with socket.create_connection((host, port), timeout=timeout) as sock:
            with insecure.wrap_socket(sock, server_hostname=host) as tls:
                return _cert_summary(tls.getpeercert(binary_form=True))

    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            with _check_context().wrap_socket(sock, server_hostname=host) as tls:
                result.update(status="ok", detail="certificate trusted",
                              **_cert_summary(tls.getpeercert(binary_form=True)))
    except ssl.SSLCertVerificationError as exc:
        result.update(status=_VERIFY_CODES.get(exc.verify_code, "untrusted"),
                      detail=exc.verify_message or str(exc))
        try:
            result.update(peer_cert())
        except Exception:  # noqa: BLE001 — the verdict above is what matters
            pass
    except (OSError, ssl.SSLError) as exc:
        detail = f"{type(exc).__name__}: {exc}"
        if proxied:
            detail += " (direct connection; the service reaches this host via HTTPS_PROXY)"
        result.update(status="unreachable", detail=detail)
    result["ok"] = result["status"] == "ok"
    return result


def check_all(timeout: float = 5.0) -> dict:
    """Policy summary plus :func:`check_endpoint` for every configured https URL."""
    mode, bundle, verify = _resolve()
    certs = 0
    if bundle:
        try:
            with open(bundle, encoding="utf-8", errors="replace") as fh:
                certs = fh.read().count("BEGIN CERTIFICATE")
        except OSError:
            pass
    endpoints = []
    for env, url in https_endpoints_from_env().items():
        endpoints.append({"env": env, **check_endpoint(url, timeout)})
    return {
        "mode": mode,
        "bundle": bundle,
        "bundle_certs": certs,
        "verifying": verify is not False,
        "endpoints": endpoints,
    }
