"""Pydantic-AI adapter for LLMFactory.

Exposes the public API: `get_llm_for_pydantic_ai`, `get_embedding_config`,
`get_embedding`, `get_embeddings_batch`, `get_embedding_client`. Internally
delegates to `builder.build_pydantic_ai_model` and `core.factory.LLMFactory`.

The `pydantic_ai` package is loaded lazily — only when a caller actually uses
``vertex_proxy/<model>`` (which needs `VertexProxyModel`) or explicitly imports
one of the ``VertexProxy*`` symbols. Services that only call ``get_embedding*``
or use string-based providers do not need ``pydantic_ai`` installed.
"""

from __future__ import annotations

from typing import Any, Dict, List

from ...core.factory import LLMFactory
from ...core.introspect import (
    get_embedding_config_sync,
    pydantic_ai_model_string,
    probe_embedding_dimension,
    validate_embedding_config,
)
from .builder import build_pydantic_ai_model


def get_llm_for_pydantic_ai(reasoner: bool = False, **kwargs):
    """Returns a pydantic-ai model derived from LLM_CONNECTOR_TYPE + LLM_TYPE.

    For vertex_proxy, returns a `VertexProxyModel` instance with custom
    Raw Connector authentication. For other providers, returns a pydantic-ai
    model string.

    Args:
        reasoner: If True, use a reasoning model (currently ignored — set via
            LLM_CONNECTOR_TYPE + LLM_TYPE).
        **kwargs: Additional arguments (ignored for compatibility).
    """
    return build_pydantic_ai_model()


def get_embedding_config() -> Dict[str, Any]:
    """Returns ``{provider, model, dimension, api_type}`` for the configured embedder."""
    return get_embedding_config_sync()


def get_embedding(text: str) -> List[float]:
    """Synchronous wrapper around the async embedding client. Returns a single vector."""
    from shared.async_utils import run_async

    # run_async: one long-lived loop for every sync caller, not asyncio.run per call.
    return run_async(LLMFactory.embedding_client().embedding(text))[0]


def get_embeddings_batch(texts: List[str]) -> List[List[float]]:
    """Synchronous wrapper around the async embedding client for batch operations."""
    from shared.async_utils import run_async

    return run_async(LLMFactory.embedding_client().embedding(texts))


def get_embedding_client():
    """Returns the LLMFactory embedding client directly."""
    return LLMFactory.embedding_client()


# Lazy access to symbols that require the `pydantic_ai` SDK at import time.
# Keeps `from shared.llm_factory.adapters.pydantic_ai import get_embedding`
# working in services that don't have pydantic_ai installed.
_LAZY_VERTEX_PROXY = {
    "VertexProxyAPIError",
    "VertexProxyAuthenticationError",
    "VertexProxyConnectionError",
    "VertexProxyError",
    "VertexProxyModel",
    "VertexProxyRateLimitError",
}


def __getattr__(name: str):
    if name in _LAZY_VERTEX_PROXY:
        from . import vertex_proxy_model as _vpm
        return getattr(_vpm, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "get_llm_for_pydantic_ai",
    "get_embedding_config",
    "get_embedding",
    "get_embeddings_batch",
    "get_embedding_client",
    "build_pydantic_ai_model",
    "pydantic_ai_model_string",
    "probe_embedding_dimension",
    "validate_embedding_config",
    "VertexProxyModel",
    "VertexProxyError",
    "VertexProxyConnectionError",
    "VertexProxyAuthenticationError",
    "VertexProxyRateLimitError",
    "VertexProxyAPIError",
]
