"""The converter LLM: markdown-api's own LLM, used when a job brings none.

It is this pod's env, under the names spaces use — ``LLM_CONNECTOR_TYPE`` +
``LLM_TYPE``, ``PROXY_BASE_URL``, ``PROXY_CONNECTOR_ID``, ``PROXY_AUTH_TOKEN``,
``GOOGLE_CLOUD_PROJECT_ID``, ``VERTEX_LOCATION`` — whatever type that is. In the
product it comes from Helm and each space sends its own; changing it at runtime
(LLM_RUNTIME_CHANGE, PUT /llm) was removed in 1.4.8.5.
"""

from __future__ import annotations

from typing import Optional

FIELDS = ("model", "base_url", "connector_id", "auth_token", "project", "location")


def env_default() -> dict:
    """The env default, as the fields a connector has."""
    from shared.connector_credentials import global_connector_creds

    from app.converters import vlm_lane

    g = global_connector_creds()
    return {"model": vlm_lane.pod_model(), "base_url": g.base_url,
            "connector_id": g.connector_id, "auth_token": g.auth_token,
            "project": g.project, "location": g.location}


def describe() -> dict:
    """What GET /llm shows: the converter LLM in use, never the token itself."""
    try:
        current = env_default()
        error = None
    except Exception as exc:  # noqa: BLE001 - an unconfigured pod still answers GET
        current, error = {}, str(exc).splitlines()[0]
    shown = {k: current.get(k) or None for k in FIELDS if k != "auth_token"}
    return {"source": "env", **shown,
            "token_set": bool(current.get("auth_token")),
            **({"error": error} if error else {})}


def job_lane(sent: Optional[dict]) -> Optional[dict]:
    """The lane a job runs on: what the caller sent, else None — the env default."""
    return sent or None
