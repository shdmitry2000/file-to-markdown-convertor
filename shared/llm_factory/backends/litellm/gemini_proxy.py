"""Gemini via the Raw Connector proxy.

The Raw Connector exposes a Gemini `{version}/models/{model}:generateContent`
endpoint at (v1, per the connector guide):
    {PROXY_BASE_URL}/api/connectors/{PROXY_CONNECTOR_ID}/{version}/

LiteLLM's `gemini/` provider understands the Gemini REST shape, so we point
its `api_base` at the connector URL and let it handle request/response
marshalling.

Auth: the connector typically requires a bearer token passed via
`PROXY_AUTH_TOKEN` (sent as Authorization header). Check the connector's
documentation for the exact auth scheme.

Troubleshooting: if you see an HTML 404 from ``litellm`` (``GeminiException``), the
connector path is wrong or the service is not the Raw Connector — use
``LLM_CONNECTOR_TYPE=gemini`` + ``LLM_TYPE=<model>`` with ``GOOGLE_API_KEY`` (AI Studio direct),
or fix ``PROXY_BASE_URL`` / ``PROXY_CONNECTOR_ID``.
"""

from __future__ import annotations

import os

from ...core import config
from .litellm_client import LiteLLMClient


def build_gemini_proxy_client(model: str | None = None) -> LiteLLMClient:
    """Construct a LiteLLMClient pointed at the Raw Connector Gemini endpoint.

    Reads from env:
        PROXY_BASE_URL         (required) e.g. https://ai-connector-platform.run.app
        PROXY_CONNECTOR_ID     (required) e.g. 68b451c752f2df2a9e91e0ae
        PROXY_AUTH_TOKEN       (optional) bearer token for the connector

    The final api_base sent to LiteLLM is:
        {PROXY_BASE_URL}/api/connectors/{PROXY_CONNECTOR_ID}/v1

    Args:
        model: Gemini model name without the ``gemini/`` prefix; if omitted,
            uses :func:`shared.llm_factory.core.config.default_gemini_model_id`.
    """
    mid = (model or "").strip() or config.default_gemini_model_id()
    base = os.environ.get("PROXY_BASE_URL")
    connector = os.environ.get("PROXY_CONNECTOR_ID")
    if not base or not connector:
        raise RuntimeError(
            "gemini_proxy requires PROXY_BASE_URL and PROXY_CONNECTOR_ID env vars. "
            "Set both to route Gemini calls through the Raw Connector."
        )

    root = config.raw_connector_root_from_env(base, connector)
    api_base = f"{root}/{config.vertex_proxy_studio_api_version()}"

    headers: dict[str, str] = {}
    token = os.environ.get("PROXY_AUTH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    api_key = os.environ.get("GOOGLE_API_KEY") or "proxy-no-key"

    return LiteLLMClient(
        model=f"gemini/{mid}",
        api_base=api_base,
        api_key=api_key,
        extra_headers=headers or None,
    )
