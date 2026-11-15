"""LLMFactory — the single entry point.

Parses a provider string (`anthropic/claude-sonnet-5-5`, `openai/gpt-4o-mini`,
`gemini_proxy/<model>`, ...) and returns a concrete `LLMClient` from the
litellm backend.

Usage:
    from shared.llm_factory import LLMFactory

    client = LLMFactory.from_env()            # reads LLM_CONNECTOR_TYPE + LLM_TYPE
    client = LLMFactory.build("openai/gpt-4o-mini")
    result = await client.json("Return {\\"ok\\": true}")
    print(result.data, result.cost_usd)
"""

from __future__ import annotations

import os

from . import config
from .protocol import LLMClient


def _resolved_embedding_model() -> str | None:
    """The per-space/template embedding model bound to this request, if any.

    Reads the 3-tier connector resolved at request bind time (template > space).
    None outside a bound request (e.g. the evaluator) → callers fall back to the
    ``EMBEDDING_TYPE`` env default."""
    try:
        from shared.request_context import get_current_resolved_connector
        resolved = get_current_resolved_connector()
    except Exception:
        return None
    return resolved.embedding_model if resolved else None


class LLMFactory:
    """Stateless factory. No instantiation needed — use the classmethods."""

    # Embedding clients are rebuilt on every retrieval (hot path). They are
    # immutable config holders, so cache them by resolved spec to skip the
    # per-call construction. Keyed by spec string, so switching
    # EMBEDDING_TYPE still yields a fresh client.
    _embedding_client_cache: dict[str, LLMClient] = {}

    @classmethod
    def from_env(cls) -> LLMClient:
        """Build a client from `LLM_CONNECTOR_TYPE` + `LLM_TYPE`."""
        provider = config.required_provider()
        return cls.build(provider)

    @classmethod
    def build(cls, provider: str, creds=None) -> LLMClient:
        """Build a client from an explicit provider string.

        Routes through the litellm backend (LiteLLM and direct SDK clients).

        Provider string format is ``<scheme>[/<model>]``. Schemes recognised:
            google_genai[/<model>]     — google-genai SDK direct
            gemini_proxy[/<model>]     — Raw Connector v1beta
            vertex_proxy[/<model>]     — Raw Connector Vertex AI format
            vertex_ai/<model>          — Vertex AI via LiteLLM (litellm backend)
            gemini/<model>             — LiteLLM gemini provider
            openai/<model>             — OpenAI
            anthropic/<model>          — Anthropic
            <anything else>/<model>    — delegated to backend

        ``creds``: a space's connector, whose own key / project satisfy the checks
        (the clients then send them per call); default = the request-bound one.
        """
        provider = provider.strip()
        if not provider:
            raise ValueError("provider string is empty")

        from .provider_profiles import validate_provider

        problems = validate_provider(provider, creds)
        if problems:
            raise ValueError(
                f"LLM provider '{provider}' is misconfigured — "
                "set the required parameters (see shared/llm_factory provider "
                f"profiles):\n  - " + "\n  - ".join(problems)
            )

        from ..backends.litellm import build_litellm_client
        return build_litellm_client(provider)

    @classmethod
    def embedding_client(cls) -> LLMClient:
        """Return a client suitable for embeddings.

        Reads `EMBEDDING_TYPE` (if set) or falls back to the
        primary provider. Embeddings via `anthropic` raise
        NotImplementedError, so callers using those as primary must set
        ``EMBEDDING_TYPE`` to a model that supports embeddings
        (e.g. ``vertex_proxy/gemini-embedding-001``,
        ``openai/text-embedding-3-small``).
        """
        spec = cls.embedding_spec()
        client = cls._embedding_client_cache.get(spec)
        if client is None:
            client = cls.build_embedding(spec)
            cls._embedding_client_cache[spec] = client
        return client

    @classmethod
    def embedding_spec(cls) -> str:
        """The embedding provider/model this request embeds with."""
        return (
            _resolved_embedding_model()
            or config.embedding_from_env()
            or config.provider_from_env()
            or config.required_provider()
        )

    @classmethod
    def build_embedding(cls, spec: str) -> LLMClient:
        """A client whose EMBEDDING model is the one ``spec`` names.

        ``build`` reads ``<scheme>/<model>`` as a completion model and leaves the
        embedding model to each backend's own default, so ``openai/<private-model>``
        embedded with text-embedding-3-small and ``vertex_proxy/<model>`` with
        gemini-embedding-001 — whatever the spec said. Provider-independent: any
        scheme, any model name.
        """
        return as_embedding_client(cls.build(spec), spec)

    @classmethod
    def reset_embedding_client_cache(cls) -> None:
        """Drop memoized embedding clients (tests / config reloads)."""
        cls._embedding_client_cache.clear()


def as_embedding_client(client, spec: str):
    """Point ``client``'s embedding model at the model ``spec`` names (if it has one).

    Each client knows its own form — litellm routes by the provider prefix and keeps
    ``openai/<model>`` whole, the proxy client wants the bare model id — so a client
    that defines ``set_embedding_spec`` decides; otherwise the bare model is used."""
    from .query_cache import install

    _, _, model = (spec or "").partition("/")
    if not model.strip():
        return install(client, (spec or "").strip())
    if hasattr(client, "set_embedding_spec"):
        client.set_embedding_spec(spec.strip())
    elif hasattr(client, "_embedding_model"):
        client._embedding_model = model.strip()
    return install(client, spec.strip())
