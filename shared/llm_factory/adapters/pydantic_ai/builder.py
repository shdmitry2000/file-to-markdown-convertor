"""Pydantic-AI model builder for LLMFactory providers.

Builds the right pydantic-ai `Model` for the configured `LLM_CONNECTOR_TYPE` + `LLM_TYPE`.
For `vertex_proxy` we use a custom `VertexProxyModel` (custom auth via Raw Connector
bearer token); for everything else we return a pydantic-ai-native model string.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from ...core import config


def _vertex_location_for_model(model: str, location: str | None = None) -> str:
    """Return the Vertex region a model must use.
    
    gemini-3.x is currently served only on the `global` endpoint (not the
    regional ones like us-central1). Everything else uses the env default.
    
    Same logic as LiteLLMClient.vertex_location_for().
    """
    if model.startswith("gemini-3"):
        return "global"
    return location or os.environ.get("VERTEX_LOCATION") or os.environ.get("VERTEXAI_LOCATION") or "us-central1"


def build_pydantic_ai_model(provider: str | None = None, creds: Mapping[str, Any] | None = None) -> Any:
    """Build a pydantic-ai model from LLM_CONNECTOR_TYPE + LLM_TYPE.

    Args:
        provider: Optional explicit provider string (e.g., "vertex_proxy/gemini-3.5-flash").
            When omitted, reads LLM_CONNECTOR_TYPE + LLM_TYPE.
        creds: Optional credentials for this model (base_url, connector_id, auth_token,
            project, location; the scheme's own, as provider_profiles names them). A field
            left empty is read from the environment, as without creds.

    Returns:
        A model string suitable for pydantic-ai Agent, GoogleModel with ADC for vertex_ai,
        or a `VertexProxyModel` instance for vertex_proxy.

    Raises:
        RuntimeError: If vertex_proxy is selected but required env vars are missing.
    """
    spec = (provider or config.required_provider()).strip()
    if not spec:
        raise RuntimeError("LLM_CONNECTOR_TYPE + LLM_TYPE are not set")

    scheme, _, model = spec.partition("/")
    model = model.strip() or config.default_gemini_model_id()
    given = {k: v for k, v in (creds or {}).items() if v}

    # Handle vertex_ai specially - use GoogleCloudProvider with ADC
    if scheme == "vertex_ai":
        try:
            from pydantic_ai.models.google import GoogleModel
            from pydantic_ai.providers.google_cloud import GoogleCloudProvider

            # Get project ID for Vertex AI
            project_id = (
                given.get("project") or
                os.environ.get("GOOGLE_CLOUD_PROJECT_ID") or
                os.environ.get("GOOGLE_CLOUD_PROJECT") or
                os.environ.get("VERTEXAI_PROJECT")
            )
            if not project_id:
                raise RuntimeError(
                    "vertex_ai requires GOOGLE_CLOUD_PROJECT_ID or similar env var"
                )
            
            # Determine location: gemini-3.x models require "global" endpoint
            # (same logic as LiteLLMClient.vertex_location_for)
            location = _vertex_location_for_model(model, given.get("location"))
            
            # Google Cloud (Vertex AI) with ADC. GoogleProvider(vertexai=True, ...) is
            # deprecated in pydantic-ai and goes in v2.0.
            provider_instance = GoogleCloudProvider(
                project=project_id,
                location=location
            )
            # Thinking off, as on every lane (core/generation.py): without it Gemini
            # thinks by default on every agent turn.
            from pydantic_ai.models.google import GoogleModelSettings

            return GoogleModel(model, provider=provider_instance, settings=GoogleModelSettings(
                google_thinking_config={"thinking_budget": 0}))
        except ImportError as e:
            raise RuntimeError(
                f"pydantic-ai Google integration not available: {e}. "
                "Install with: pip install 'pydantic-ai[google-vertex]'"
            )

    if scheme == "vertex_proxy":
        from .vertex_proxy_model import VertexProxyModel

        base_url = given.get("base_url") or os.environ.get("PROXY_BASE_URL")
        connector_id = given.get("connector_id") or os.environ.get("PROXY_CONNECTOR_ID")
        token = given.get("auth_token") or os.environ.get("PROXY_AUTH_TOKEN")
        project_id = given.get("project") or config.gcp_project_id_for_vertex_proxy()
        location = given.get("location") or os.environ.get("VERTEX_LOCATION", "us-central1")

        if not all([base_url, connector_id, token]):
            raise RuntimeError(
                "vertex_proxy requires PROXY_BASE_URL, PROXY_CONNECTOR_ID, PROXY_AUTH_TOKEN"
            )

        return VertexProxyModel(
            base_url=base_url,
            connector_id=connector_id,
            auth_token=token,
            project_id=project_id,
            location=location,
            model_name=model
        )

    if given.get("auth_token"):   # a key of its own (openai, anthropic, gemini): not the env one
        return _model_with_key(scheme, model, given["auth_token"])

    from ...core.introspect import pydantic_ai_model_string
    return pydantic_ai_model_string(spec)


def _model_with_key(scheme: str, model: str, api_key: str) -> Any:
    """A key-authenticated model with the given key instead of the scheme's env variable."""
    if scheme == "openai":
        from pydantic_ai.models.openai import OpenAIChatModel
        from pydantic_ai.providers.openai import OpenAIProvider

        return OpenAIChatModel(model, provider=OpenAIProvider(api_key=api_key))
    if scheme == "anthropic":
        from pydantic_ai.models.anthropic import AnthropicModel
        from pydantic_ai.providers.anthropic import AnthropicProvider

        return AnthropicModel(model, provider=AnthropicProvider(api_key=api_key))
    if scheme in ("gemini", "google_genai"):
        from pydantic_ai.models.google import GoogleModel
        from pydantic_ai.providers.google import GoogleProvider

        return GoogleModel(model, provider=GoogleProvider(api_key=api_key))
    raise RuntimeError(f"a key of its own is not supported for {scheme!r}")


__all__ = ["build_pydantic_ai_model"]
