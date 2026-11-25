"""Environment variable contract for shared/llm_factory.

The model: LLM_CONNECTOR_TYPE + LLM_TYPE, EMBEDDING_TYPE (same connection type).
Identity: PROXY_BASE_URL / PROXY_CONNECTOR_ID / PROXY_AUTH_TOKEN on the proxy lane;
GOOGLE_CLOUD_PROJECT_ID / VERTEX_LOCATION on vertex_ai. Generation settings live in
generation.py. Everything else is a constant here (1.4.8.5 removed the rest).
"""

from __future__ import annotations

import os

from shared.utils.tls import httpx_verify

def default_gemini_model_id() -> str:
    """The model name of LLM_TYPE (the ``/<model>`` part of the configured spec).

    Raises:
        ValueError: if LLM_TYPE is not set.
    """
    model = split_spec(provider_from_env())[1]
    if model:
        return model
    raise ValueError("Set LLM_CONNECTOR_TYPE + LLM_TYPE (e.g. vertex_proxy + gemini-3.8-flash).")


def default_embedding_model_id() -> str:
    """The model name of EMBEDDING_TYPE, else gemini-embedding-001."""
    return split_spec(embedding_from_env())[1] or "gemini-embedding-001"


def raw_connector_root_from_env(base_url: str, connector_id: str) -> str:
    """``{PROXY_BASE_URL}/api/connectors/{id}`` (no trailing slash)."""
    return f"{base_url.rstrip('/')}/api/connectors/{connector_id}"


def gcp_project_id_for_vertex_proxy() -> str | None:
    """GCP project id for Vertex-shaped Raw Connector URLs.

    Reads ``GOOGLE_CLOUD_PROJECT_ID``, then ``GOOGLE_CLOUD_PROJECT``.
    """
    for key in (ENV_GOOGLE_CLOUD_PROJECT_ID, ENV_GOOGLE_CLOUD_PROJECT):
        raw = os.environ.get(key)
        if not raw:
            continue
        v = raw.strip().strip('"').strip("'")
        if v:
            return v
    return None


ENV_VERTEXAI_PROJECT = "VERTEXAI_PROJECT"
ENV_VERTEXAI_LOCATION = "VERTEXAI_LOCATION"


def ensure_litellm_vertex_env_from_gcp_env() -> None:
    """Set LiteLLM Vertex env vars from common GCP names if missing.

    LiteLLM expects ``VERTEXAI_PROJECT`` and ``VERTEXAI_LOCATION``. Many repos only
    set ``GOOGLE_CLOUD_PROJECT_ID`` / ``VERTEX_LOCATION``.
    """
    if not (os.environ.get(ENV_VERTEXAI_PROJECT) or "").strip():
        pid = gcp_project_id_for_vertex_proxy()
        if pid:
            os.environ[ENV_VERTEXAI_PROJECT] = pid
    if not (os.environ.get(ENV_VERTEXAI_LOCATION) or "").strip():
        loc = (os.environ.get("VERTEX_LOCATION") or "us-central1").strip()
        if loc:
            os.environ[ENV_VERTEXAI_LOCATION] = loc


# ── Split style: connection type + model name ─────────────────────────
ENV_LLM_CONNECTOR_TYPE = "LLM_CONNECTOR_TYPE"
"""Connection type of the answer model (vertex_proxy, vertex_ai, openai, anthropic,
google_genai, …) — decides which credentials are sent."""
ENV_LLM_TYPE = "LLM_TYPE"
"""Answer model name for that connection type (e.g. gemini-3.8-flash)."""
ENV_EMBEDDING_TYPE = "EMBEDDING_TYPE"
"""Embedding model name on the LLM_CONNECTOR_TYPE connection (e.g. gemini-embedding-001)."""


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip().strip('"').strip("'")


def compose_spec(connector_type: str | None, model: str | None) -> str:
    """``type/model`` — the form the factory and litellm take. Either part may be
    empty: no type → the model as given; no model → the type alone (claude-style
    types that need no model id)."""
    t, m = (connector_type or "").strip(), (model or "").strip()
    if not t:
        return m
    return f"{t}/{m}" if m else t


def split_spec(spec: str | None) -> tuple[str, str]:
    """``(connector_type, model)`` from a combined ``type/model`` string. A bare value
    that is not a known type is a model name with no type."""
    from .provider_profiles import PROVIDER_PROFILES

    s = (spec or "").strip().strip('"').strip("'")
    if "/" in s:
        t, _, m = s.partition("/")
        return t.strip(), m.strip()
    return (s, "") if s in PROVIDER_PROFILES else ("", s)


def provider_from_env() -> str:
    """The answer model as ``type/model`` (LLM_CONNECTOR_TYPE + LLM_TYPE); empty when
    LLM_CONNECTOR_TYPE is not set."""
    if _env(ENV_LLM_CONNECTOR_TYPE):
        return compose_spec(_env(ENV_LLM_CONNECTOR_TYPE), _env(ENV_LLM_TYPE))
    return ""


def embedding_from_env() -> str:
    """The embedding model as ``type/model``: EMBEDDING_TYPE on the answer model's
    connection type; empty when EMBEDDING_TYPE is not set."""
    if _env(ENV_EMBEDDING_TYPE):
        return compose_spec(split_spec(provider_from_env())[0], _env(ENV_EMBEDDING_TYPE))
    return ""


# ── Raw Connector proxy ───────────────────────────────────────────────
ENV_PROXY_BASE_URL = "PROXY_BASE_URL"
ENV_PROXY_CONNECTOR = "PROXY_CONNECTOR_ID"
ENV_PROXY_AUTH_TOKEN = "PROXY_AUTH_TOKEN"

ENV_GOOGLE_CLOUD_PROJECT_ID = "GOOGLE_CLOUD_PROJECT_ID"
ENV_GOOGLE_CLOUD_PROJECT = "GOOGLE_CLOUD_PROJECT"
"""Both set the GCP project id on the vertex_ai lane; ``*_PROJECT_ID`` wins."""

# The proxy URL shape, per the connector guide (settings until 1.4.8.5; every lane set
# exactly these): generate ``v1/models/{m}:generateContent`` (studio, no project or
# region — the connector supplies them; streaming ``…:streamGenerateContent?alt=sse``),
# embeddings ``v1/models/{m}:predict``, body ``{"instances": [{"content": ...}]}``, up
# to 250 texts per request.


def vertex_proxy_studio_api_version() -> str:
    return "v1"


def vertex_proxy_studio_path(model: str, action: str) -> str:
    """``{version}/models/{model}:{action}`` — the Gemini-API path, no project/region."""
    return f"{vertex_proxy_studio_api_version()}/models/{model}:{action}"


# ── Provider-native API keys (read by LiteLLM, listed here for docs) ──
KNOWN_API_KEY_ENVS = (
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GOOGLE_API_KEY",      # used by `gemini/`
    "GEMINI_API_KEY",      # alt name, also read
    "GROQ_API_KEY",
    "MISTRAL_API_KEY",
    "COHERE_API_KEY",
    "AZURE_API_KEY",
)

# ── No default provider ───────────────────────────────────────────────

class LLMProviderNotConfigured(RuntimeError):
    """``LLM_CONNECTOR_TYPE`` + ``LLM_TYPE`` are not set. Raised instead of picking a lane."""


def required_provider() -> str:
    """The configured provider string, or raise.

    There was a ``DEFAULT_PROVIDER = "vertex_ai/gemini-2.5-flash"`` here, used
    wherever no provider was set. It made a missing setting
    invisible: a pod whose ConfigMap failed to arrive — or that was never given
    the variable — came up healthy and answered through Vertex AI directly, which
    is the wrong lane everywhere the proxy is the lane and is unreachable at all
    on an air-gapped cluster. The failure then surfaced far from its cause, as a
    credentials or network error inside google-auth, naming a provider nobody
    configured.

    A guess is not a default. There is no lane that is right for every
    deployment, so an unset provider is a configuration error and is reported as
    one — at the point of use, naming the variable to set.

    Deliberately NOT a startup check. A service with no provider configured can
    still serve everything that does not call an LLM, and refusing to boot would
    take those down too; the LLM call is where the setting is actually needed, so
    that is where its absence is raised.
    """
    spec = provider_from_env()
    if not spec:
        raise LLMProviderNotConfigured(
            f"{ENV_LLM_CONNECTOR_TYPE} + {ENV_LLM_TYPE} "
            f"are not set, and there is no default: the right lane "
            f"differs per deployment, so guessing one sends traffic somewhere "
            f"nobody chose. Set them to a type and a model — "
            f"vertex_proxy + gemini-3.5-flash for a Raw Connector lane, "
            f"vertex_ai + gemini-3.5-flash for Vertex direct (needs a GCP project "
            f"and ADC), openai + gpt-4o-mini, anthropic + claude-3-7-sonnet. In a "
            f"cluster it arrives from the chart's ConfigMap via envFrom, so an "
            f"absent or renamed ConfigMap delivers none of it."
        )
    return spec

# TLS for outbound proxy/LLM HTTP is owned by shared/utils/tls.py (TLS_VERIFY
# auto|strict|off, the CA bundle, the legacy SSL_VERIFY); re-exported here for
# this package's clients.
tls_verify = httpx_verify
