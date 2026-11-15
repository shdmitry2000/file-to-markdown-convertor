"""Pydantic-AI model builder for LLMFactory providers.

Builds the right pydantic-ai `Model` for the configured `LLM_CONNECTOR_TYPE` + `LLM_TYPE`.
For `vertex_proxy` we use a custom `VertexProxyModel` (custom auth via Raw Connector
bearer token); for everything else we return a pydantic-ai-native model string.
"""

from __future__ import annotations

import os
from typing import Any

from ...core import config


def _vertex_location_for_model(model: str) -> str:
    """Return the Vertex region a model must use.
    
    gemini-3.x is currently served only on the `global` endpoint (not the
    regional ones like us-central1). Everything else uses the env default.
    
    Same logic as LiteLLMClient.vertex_location_for().
    """
    if model.startswith("gemini-3"):
        return "global"
    return os.environ.get("VERTEX_LOCATION") or os.environ.get("VERTEXAI_LOCATION") or "us-central1"


def build_pydantic_ai_model(provider: str | None = None) -> Any:
    """Build a pydantic-ai model from LLM_CONNECTOR_TYPE + LLM_TYPE.

    Args:
        provider: Optional explicit provider string (e.g., "vertex_proxy/gemini-2.5-flash").
            When omitted, reads LLM_CONNECTOR_TYPE + LLM_TYPE.

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

    # Handle vertex_ai specially - use GoogleProvider with ADC
    if scheme == "vertex_ai":
        try:
            from pydantic_ai.models.google import GoogleModel
            from pydantic_ai.providers.google import GoogleProvider
            
            # Get project ID for Vertex AI
            project_id = (
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
            location = _vertex_location_for_model(model)
            
            # Create provider with Vertex AI mode and ADC
            provider_instance = GoogleProvider(
                vertexai=True,
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

        base_url = os.environ.get("PROXY_BASE_URL")
        connector_id = os.environ.get("PROXY_CONNECTOR_ID")
        token = os.environ.get("PROXY_AUTH_TOKEN")
        project_id = config.gcp_project_id_for_vertex_proxy()
        location = os.environ.get("VERTEX_LOCATION", "us-central1")

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

    from ...core.introspect import pydantic_ai_model_string
    return pydantic_ai_model_string(spec)


__all__ = ["build_pydantic_ai_model"]
