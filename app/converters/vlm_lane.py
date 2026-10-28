"""Which LLM lane this job's VLM calls take: the caller's connector, or the pod's env.

A /convert caller may send ``llm_connector`` — the connector its own project uses:

    {"model": "vertex_proxy/gemini-3.5-flash",            # its type picks the lane
     "base_url": ..., "connector_id": ..., "auth_token": ...,  # optional identity
     "project": ..., "location": ...}                      # optional

The connector decides HOW the VLM is reached (type + credentials), never WHAT it
produces: on a Gemini lane the model stays the pod's VLM_MODEL (the one proven
exact on table digits), so a document converts the same whoever asked and its
markdown can be shared between projects. A non-Gemini type keeps the caller's own
model, since a Gemini name means nothing there. Without a connector the pod's env
lane applies, as before.

The connector holds a token: it lives in memory for one job only, and is never
logged, stored, or written into the output.

A worker process runs one job at a time; ``job()`` scopes the connector to it and
the VLM call sites ask ``client()`` (threads inside the job see the same value).
"""

from __future__ import annotations

import hashlib
import json
import os
from contextlib import contextmanager
from typing import Any, Optional

_GEMINI_SCHEMES = {"vertex_proxy", "vertex_ai", "gemini", "gemini_proxy", "google_genai"}

_job: Optional[dict] = None
_clients: dict[str, Any] = {}


@contextmanager
def job(connector: Optional[dict]):
    """Use ``connector`` for the VLM calls made inside this block."""
    global _job
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
    (or ``vertex_proxy/<name>`` when a model name is asked for)."""
    scheme = connector_model.partition("/")[0]
    if scheme not in _GEMINI_SCHEMES:
        return connector_model
    return f"{scheme}/{name or pod_model().rpartition('/')[2]}"


def client(name: Optional[str] = None):
    """The client for this job's connector, or None (use the pod's env lane).
    ``name``: a model name to use on that lane instead of the pod's."""
    conn = _job
    if not conn or not conn.get("model"):
        return None
    key = hashlib.sha256(json.dumps([conn, name], sort_keys=True).encode()).hexdigest()
    if key not in _clients:
        from shared.connector_credentials import ConnectorCreds
        from shared.space_llm import pinned_client

        spec = vlm_spec(conn["model"], name)
        creds = ConnectorCreds(
            base_url=conn.get("base_url") or "",
            connector_id=conn.get("connector_id") or "",
            auth_token=conn.get("auth_token") or "",
            # Identity sent by the caller is its project's own (space/template tier),
            # which is what lets pinned_client use it for the matching type only.
            source="space" if conn.get("auth_token") else "global",
            project=conn.get("project"), location=conn.get("location"), model=spec,
        )
        _clients[key] = pinned_client(creds, spec)
    return _clients[key]
