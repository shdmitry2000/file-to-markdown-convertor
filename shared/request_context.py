"""Request-scoped context propagated via :mod:`contextvars`.

The active ``space`` is set at the request boundary (the rag_core indexer /
retrieval routers, and the multitenancy middleware). Code deep in the call stack
that has no access to the ``Request`` — notably the LLM factory's vertex_proxy
lane — reads :data:`current_space` to resolve per-space connector credentials.

contextvars propagate across ``await`` within the same task, so a value set in a
FastAPI dependency is visible to the endpoint and everything it awaits.
"""

from __future__ import annotations

import contextvars
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from shared.connector_credentials import ConnectorCreds, TemplateConnectorConfig

current_space: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "current_space", default=None
)


def set_current_space(space: Optional[str]) -> None:
    current_space.set(space or None)


def get_current_space() -> Optional[str]:
    return current_space.get()


# The template's OWN connector config, bound by the template routers. Typed via
# TYPE_CHECKING (no runtime import of shared.connector_credentials → no cycle). The
# 3-tier resolver reads it as the top (template-wins) tier.
current_template_connector: contextvars.ContextVar[Optional["TemplateConnectorConfig"]] = (
    contextvars.ContextVar("current_template_connector", default=None)
)


def set_current_template_connector(cfg: Optional["TemplateConnectorConfig"]) -> None:
    current_template_connector.set(cfg)


def get_current_template_connector() -> Optional["TemplateConnectorConfig"]:
    return current_template_connector.get()


# The fully-resolved 3-tier connector (template > space > global), computed once
# at request bind time. Lets sync callers (the embedding client factory) read the
# per-space model/embedding without an async DB round-trip. None outside a bound
# request (e.g. the evaluator) → callers fall back to env.
current_resolved_connector: contextvars.ContextVar[Optional["ConnectorCreds"]] = (
    contextvars.ContextVar("current_resolved_connector", default=None)
)


def set_current_resolved_connector(creds: Optional["ConnectorCreds"]) -> None:
    current_resolved_connector.set(creds)


def get_current_resolved_connector() -> Optional["ConnectorCreds"]:
    return current_resolved_connector.get()
