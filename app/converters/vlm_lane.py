"""Which LLM this job's calls use — every LLM call in the converter asks ``client()``.

A /convert caller may send ``llm`` (or, before 1.4.9, ``llm_connector``)::

    {"mode": "space" | "params",
     "model": "vertex_proxy/gemini-3.8-flash",               # type/model
     "base_url": ..., "connector_id": ..., "auth_token": ...,  # identity
     "project": ..., "location": ...}

- **space**: the space's own connector, for every call in the job. Nothing comes from
  this pod's env; a connector without the fields its type needs fails the job before
  any page work, naming what is missing.
- **params** (also: no ``llm`` at all): this pod's lane — ``VLM_MODEL``, else the
  ``LLM_*`` env — with any field the request sends taking precedence. A request that
  sends a token brings its whole identity; otherwise the pod's own is used.
- **no mode** (``llm_connector`` from a 1.4.7.8–1.4.8.x template): the caller's type
  with the pod's Gemini model, its identity when it sent a token, else the pod's lane.

The connector holds a token: it lives in memory for one job only, and is never
logged, stored, or written into the output.

A worker process runs one job at a time; ``job()`` scopes the connector to it and
the call sites ask ``client()`` (threads inside the job see the same value).
Clients are built on first use, so a lane that is never called is never checked.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from contextlib import contextmanager
from typing import Any, Optional

logger = logging.getLogger(__name__)

_GEMINI_SCHEMES = {"vertex_proxy", "vertex_ai", "gemini", "gemini_proxy", "google_genai"}

_job: Optional[dict] = None
_clients: dict[str, Any] = {}


class LaneError(ValueError):
    """The job's LLM lane cannot be built; the message says which field is missing."""


def check(connector: Optional[dict]) -> None:
    """Raise LaneError when a space-mode connector lacks what its type needs.

    Checked when the job starts, not at the first call: a picture description never
    fails its document, so a lane checked there would quietly drop every picture."""
    conn = connector or {}
    if conn.get("mode") != "space":
        return
    if not conn.get("model"):
        raise LaneError("space mode: the space's connector has no model")
    missing = _missing(conn, conn["model"])
    if missing:
        raise LaneError(f"space mode: the space's connector for {conn['model']} has no "
                        f"{', '.join(missing)} — set it on the space's LLM connector")


@contextmanager
def job(connector: Optional[dict]):
    """Use ``connector`` for the LLM calls made inside this block."""
    global _job
    check(connector)
    previous, _job = _job, (connector or None)
    try:
        yield
    finally:
        _job = previous


def pod_model() -> str:
    """The pod's VLM model spec: VLM_MODEL, else the factory's provider."""
    pod = os.getenv("VLM_MODEL", "").strip()
    if not pod:
        from shared.llm_factory.core import config
        pod = config.required_provider()
    return pod


def vlm_spec(connector_model: str, name: Optional[str] = None) -> str:
    """``vertex_proxy/gemini-3.5-flash`` -> ``vertex_proxy/<pod VLM model>``
    (or ``vertex_proxy/<name>`` when a model name is asked for). No-mode callers only."""
    scheme = connector_model.partition("/")[0]
    if scheme not in _GEMINI_SCHEMES:
        return connector_model
    return f"{scheme}/{name or pod_model().rpartition('/')[2]}"


def _with_name(spec: str, name: Optional[str]) -> str:
    """``spec`` with its model replaced by ``name`` (same lane), when one is asked for."""
    if not name:
        return spec
    scheme = spec.partition("/")[0] if "/" in spec else ""
    return f"{scheme}/{name}" if scheme else name


def model(name: Optional[str] = None) -> str:
    """The model spec this job's calls use (``name``: another model on the same lane)."""
    conn = _job or {}
    mode = conn.get("mode")
    if mode in ("space", "params") and conn.get("model"):
        return _with_name(conn["model"], name)
    if conn.get("model") and mode is None:
        return vlm_spec(conn["model"], name)
    return _with_name(pod_model(), name)


def _missing(conn: dict, spec: str) -> list[str]:
    """Identity fields a space connector must carry for its type (location may default)."""
    from shared.llm_factory.core.provider_profiles import PROVIDER_PROFILES, scheme_of

    profile = PROVIDER_PROFILES.get(scheme_of(spec))
    need = [c for c in (profile.credential_columns if profile else ()) if c != "location"]
    return [c for c in need if not (conn.get(c) or "").strip()]


def _creds(conn: dict, spec: str):
    from shared.connector_credentials import ConnectorCreds

    return ConnectorCreds(
        base_url=conn.get("base_url") or "",
        connector_id=conn.get("connector_id") or "",
        auth_token=conn.get("auth_token") or "",
        # The identity is the caller's own, so pinned_client uses it instead of env.
        source="space" if conn.get("auth_token") or conn.get("mode") == "space" else "global",
        project=conn.get("project"), location=conn.get("location"), model=spec,
    )


def _build(conn: dict, name: Optional[str]):
    from shared.llm_factory import LLMFactory
    from shared.space_llm import pinned_client

    mode = conn.get("mode")
    spec = model(name)
    if mode == "space":
        check(conn)
        return pinned_client(_creds(conn, spec), spec)
    if mode == "params":
        if conn.get("auth_token"):
            return pinned_client(_creds(conn, spec), spec)
        return LLMFactory.build(spec)
    if conn.get("model"):  # no mode: a 1.4.7.8–1.4.8.x caller
        try:
            return pinned_client(_creds(conn, spec), spec)
        except ValueError as exc:
            # Only the type came (the deployment's connector): this pod's env for that
            # lane is needed, e.g. PROXY_AUTH_TOKEN. Without it, use the pod's lane.
            if conn.get("auth_token"):
                raise
            logger.warning("VLM lane %s came without credentials and this pod cannot "
                           "build it (%s); using the pod's own lane",
                           spec, str(exc).splitlines()[0][:120])
    return LLMFactory.build(_with_name(pod_model(), name))


def client(name: Optional[str] = None):
    """The client for this job's lane (built once, then cached).
    ``name``: a model name to use on that lane instead of its own."""
    conn = _job or {}
    # Keyed on what is built: the spec, plus the identity only when the caller's is used
    # — so "params, no fields" and "no llm at all" share the pod's one client.
    own = conn.get("mode") == "space" or bool(conn.get("auth_token")) or (
        conn.get("mode") is None and bool(conn.get("model")))
    ident = [conn.get(k) for k in ("base_url", "connector_id", "auth_token", "project",
                                   "location")] if own else None
    key = hashlib.sha256(json.dumps([model(name), ident]).encode()).hexdigest()
    if key not in _clients:
        built = _build(conn, name)
        if not hasattr(built, "vision_sync"):
            raise LaneError(
                f"VLM needs a litellm/vertex provider with vision (got {type(built).__name__} "
                f"for {model(name)}); set VLM_MODEL / LLM_CONNECTOR_TYPE to a Gemini lane."
            )
        _clients[key] = built
    return _clients[key]
