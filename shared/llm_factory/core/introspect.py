"""LLMFactory introspection helpers.

Replaces the legacy ``shared.gemini_proxy_core.embedding_support.get_embedding_config``
contract with values derived from whatever client ``LLMFactory.embedding_client()``
returns.

The legacy helper returned a dict shaped like::

    {"provider": ..., "model": ..., "dimension": ..., "api_type": ...}

Callers used ``["model"]``, ``["dimension"]``, and (rarely) ``["provider"]``.
We preserve the same keys so existing call sites keep working.

Dimension is probed once via a tiny embedding call and cached process-wide;
this avoids hardcoding per-model dimensions and works for any provider the
factory routes to.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from .factory import LLMFactory

logger = logging.getLogger(__name__)

_DIM_CACHE: Dict[str, int] = {}


def _provider_label() -> str:
    """The provider string the factory will use for embeddings (e.g. ``vertex_ai/gemini-embedding-001``)."""
    return LLMFactory.embedding_client().provider


def _model_part(provider_label: str) -> str:
    """Drop the scheme prefix so callers get the bare model id (``gemini-embedding-001``)."""
    return provider_label.split("/", 1)[1] if "/" in provider_label else provider_label


async def probe_embedding_dimension(force: bool = False) -> int:
    """Probe the embedding model once and return its vector dimension.

    Caches the result per provider label. Pass ``force=True`` to re-probe.
    """
    label = _provider_label()
    if not force and label in _DIM_CACHE:
        return _DIM_CACHE[label]

    client = LLMFactory.embedding_client()
    vecs = await client.embedding("dim probe")
    dim = len(vecs[0]) if vecs and vecs[0] else 0
    if not dim:
        raise RuntimeError(f"Embedding probe returned empty vector for provider {label!r}")
    _DIM_CACHE[label] = dim
    logger.info(f"Probed embedding dimension: {label} -> {dim}")
    return dim


def get_embedding_config_sync() -> Dict[str, Any]:
    """Sync convenience wrapper. Returns ``{provider, model, dimension, api_type}``."""
    label = _provider_label()
    model = _model_part(label)

    if label not in _DIM_CACHE:
        # run_async keeps only the loop *detection* inside its try, so a
        # RuntimeError raised by the probe itself (an unreachable provider, an
        # empty vector) propagates as-is instead of being mistaken for "no loop
        # running" and retried with asyncio.run() on the caller's live loop.
        from shared.async_utils import run_async

        run_async(probe_embedding_dimension())

    return {
        "provider": label.split("/", 1)[0] if "/" in label else label,
        "model": model,
        "dimension": _DIM_CACHE.get(label, 0),
        "api_type": "llm_factory",
    }


def pydantic_ai_model_string(provider: Optional[str] = None) -> str:
    """Translate ``LLM_CONNECTOR_TYPE`` + ``LLM_TYPE`` into a pydantic-ai model string.

    pydantic-ai accepts shorthand model strings like ``openai:gpt-4o-mini``,
    ``google-vertex:gemini-2.5-flash``, ``google-gla:gemini-2.5-flash``, etc.

    Args:
        provider: Optional explicit provider string (e.g. ``vertex_ai/gemini-2.5-flash``).
            When omitted, reads ``LLM_CONNECTOR_TYPE`` + ``LLM_TYPE``, and raises
            ``LLMProviderNotConfigured`` if that is unset — there is no default.

    Returns:
        A pydantic-ai-compatible model string (e.g. ``google-vertex:gemini-2.5-flash``).
    """
    from . import config as _factory_config

    spec = (provider or _factory_config.required_provider()).strip()
    if not spec:
        raise RuntimeError("LLM_CONNECTOR_TYPE + LLM_TYPE are not set")

    scheme, _, model = spec.partition("/")
    model = model.strip() or _factory_config.default_gemini_model_id()

    mapping = {
        "vertex_ai": "google-vertex",
        "gemini": "google-gla",
        "google_genai": "google-gla",
        "gemini_proxy": "google-gla",
        "vertex_proxy": "google-vertex",
        "openai": "openai",
        "anthropic": "anthropic",
        "groq": "groq",
        "cohere": "cohere",
    }
    pa_scheme = mapping.get(scheme)
    if pa_scheme is None:
        raise RuntimeError(
            f"No pydantic-ai mapping for LLM_CONNECTOR_TYPE {scheme!r}. "
            "Add an entry to pydantic_ai_model_string() or set a supported provider."
        )
    return f"{pa_scheme}:{model}"


async def validate_embedding_config(
    expected_model: Optional[str] = None,
    expected_dim: Optional[int] = None,
    strict: bool = True,
) -> Dict[str, Any]:
    """Probe the configured embedding model and (optionally) assert expectations."""
    dim = await probe_embedding_dimension()
    label = _provider_label()
    model = _model_part(label)

    problems = []
    if expected_model and expected_model not in label:
        problems.append(f"expected model containing {expected_model!r}, got {label!r}")
    if expected_dim is not None and expected_dim != dim:
        problems.append(f"expected dimension {expected_dim}, got {dim}")

    if problems:
        msg = "Embedding validation failed: " + "; ".join(problems)
        if strict:
            raise RuntimeError(msg)
        logger.warning(msg)

    return {
        "provider": label.split("/", 1)[0] if "/" in label else label,
        "model": model,
        "dimension": dim,
        "api_type": "llm_factory",
    }
