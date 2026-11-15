"""Build LLM clients pinned to a space's connector — for out-of-request callers.

Request-scoped code (the answer/retrieval path) resolves per-space creds via the
``get_current_space()`` contextvar. That doesn't survive thread pools / eval-framework
internals, so out-of-request callers (the evaluator) can't rely on it.

``build_space_clients(space)`` resolves the space connector ONCE and returns fresh
clients with the creds **pinned** on the instance, so every call uses the space's
model + creds regardless of thread/loop. Pinned clients are built directly (never via
the shared embedding cache), so one space's creds can't leak to another.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class ConnectorNotConfigured(RuntimeError):
    """Raised when a space has no LLM model configured (per-space-only: no fallback)."""


@dataclass
class SpaceClients:
    completion: Any        # LLMClient for generation, creds pinned
    embedding: Any         # LLMClient for embeddings, creds pinned
    model: str
    embedding_model: str


async def resolve_space_creds(space: str):
    """Resolve ``space``'s connector (async — reads the DB). Call on the loop that
    has the request/DB context, then hand the returned creds to :func:`pinned_clients`.

    Raises :class:`ConnectorNotConfigured` when the space has no model (per-space-only:
    no global fallback)."""
    from shared.connector_credentials import resolve_llm_connector

    creds = await resolve_llm_connector(space)
    if not creds.model:
        raise ConnectorNotConfigured(
            f"space {space!r} has no LLM connector model configured")
    return creds


def pinned_clients(creds) -> SpaceClients:
    """Build fresh clients pinned to ``creds`` (sync — no DB). Safe to call inside a
    worker thread / throwaway event loop: building fresh here means a client's lazy
    per-loop resources (e.g. vertex_proxy's httpx client) bind to *that* loop, not a
    stale one. Pinned clients are never memoized in the shared embedding cache."""
    emb_spec = creds.embedding_model or creds.model
    return SpaceClients(
        completion=pinned_client(creds, creds.model),
        embedding=pinned_client(creds, emb_spec, embedding=True),
        model=creds.model, embedding_model=emb_spec,
    )


def pinned_client(creds, spec: str, embedding: bool = False):
    """One client for ``spec`` with ``creds`` pinned on it (see :func:`pinned_clients`).
    ``embedding``: ``spec`` names the embedding model (any provider)."""
    from shared.llm_factory import LLMFactory
    from shared.llm_factory.core.factory import as_embedding_client

    client = _build_pinned(creds, spec, LLMFactory)
    if embedding:
        client = as_embedding_client(client, spec)
    client.pinned_creds = creds
    return client


def _build_pinned(creds, spec: str, factory):
    """A client for ``spec``. A vertex_proxy client is built from the connector itself
    when the connector is complete: it carries its own endpoint and token, so the pod's
    PROXY_* env — which the factory insists on, and which a lane whose default provider
    is not the proxy does not have — is not involved."""
    from shared.connector_credentials import creds_serve_scheme

    scheme, _, model = (spec or "").partition("/")
    own = creds_serve_scheme(creds, scheme) and creds.is_complete()
    if scheme == "vertex_proxy" and own:
        from shared.llm_factory.backends.litellm.vertex_proxy import VertexProxyClient
        from shared.llm_factory.core import config

        emb_scheme, _, emb_model = (creds.embedding_model or "").partition("/")
        # Same fallbacks as the factory's build_vertex_proxy_client, so a bare
        # "vertex_proxy" spec gets the default model instead of an empty one.
        return VertexProxyClient(
            base_url=creds.base_url, connector_id=creds.connector_id,
            auth_token=creds.auth_token, project_id=creds.project,
            location=creds.location or "us-central1",
            model=model.strip() or config.default_gemini_model_id(),
            embedding_model=(emb_model if emb_scheme == "vertex_proxy" and emb_model
                             else config.default_embedding_model_id()),
        )
    return factory.build(spec, creds)


async def build_space_clients(space: str) -> SpaceClients:
    """Convenience for same-loop callers: resolve + build. For the eval worker-thread
    case use :func:`resolve_space_creds` (on the main loop) + :func:`pinned_clients`
    (inside the worker)."""
    return pinned_clients(await resolve_space_creds(space))


async def resolve_space_creds_or_none(space: str):
    """Fail-open variant: the space's creds, or None if unconfigured/unavailable.
    Callers (evaluator) fall back to the env factory when None."""
    try:
        return await resolve_space_creds(space)
    except Exception:  # noqa: BLE001 — never break the caller on connector resolution
        return None
