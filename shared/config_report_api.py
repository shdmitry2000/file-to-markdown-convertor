"""``GET /config-report`` — this pod's config inventory, for the admin panel.

Deliberately *not* under ``/health``: ``shared.auth_gate`` treats ``/health`` as a
public prefix, so mounting the env inventory there would serve it anonymously on
every internet-facing service. Under its own path the services that run the auth
gate (evaluator, agent) require a caller credential like any other route, and the
gateway exposes it only through its global-admin-gated admin router.

Templates run no auth middleware and are reachable only inside the cluster, which
is the same posture as the ``/template-connector`` endpoint they already serve
with its token redacted. The report contains no secret value either way.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Iterable

from shared.config_report import config_report
from shared.utils.tls import httpx_verify
from shared import timeouts

logger = logging.getLogger(__name__)

# Long enough for a loaded pod to answer, short enough that one dead module does
# not stall the whole panel. The fan-out is concurrent, so this bounds the page.
FANOUT_TIMEOUT_S = timeouts.probe()


def register_config_report(app, module: str, *, logging_route: bool = True) -> None:
    """Add ``GET /config-report`` to a FastAPI app, and ``GET``/``PUT /logging``
    (shared.logging_runtime) unless the pod reads the logging settings from the
    control-plane table itself (gateway, evaluator) or keeps its env (agent)."""
    if logging_route:
        from shared.logging_runtime import register_logging_route

        register_logging_route(app)

    @app.get("/config-report", tags=["admin"])
    async def config_report_endpoint():  # noqa: D401 - route handler
        """Which env this pod received, and from which chart release."""
        return config_report(module, app.version)


def _outbound_headers() -> dict:
    """Identity headers for the fan-out, same precedence as the HTTP retriever.

    /config-report sits behind the caller-credential middleware on every module
    that registers it, so an anonymous fan-out gets 401 back from each one. The
    admin calling the gateway is already authenticated; forward that identity
    rather than making the route public, which would also drop the internal
    service-token check.
    """
    from shared.auth_context import get_caller_bearer, get_caller_forwarded

    headers: dict = {}
    token = get_caller_bearer()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    headers.update(get_caller_forwarded())
    internal = (os.getenv("INTERNAL_SERVICE_TOKEN") or "").strip()
    if internal:
        headers["x-internal-auth"] = internal
    return headers


async def _fetch_one(url: str) -> dict:
    """Fetch one module's report. Never raises — unreachable is the answer."""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=FANOUT_TIMEOUT_S, verify=httpx_verify()) as client:
            resp = await client.get(
                f"{url.rstrip('/')}/config-report", headers=_outbound_headers()
            )
        if resp.status_code >= 400:
            return {"url": url, "reachable": True, "error": f"HTTP {resp.status_code}"}
        return {"url": url, "reachable": True, "report": resp.json()}
    except Exception as exc:  # noqa: BLE001 — unreachable is what the caller acts on
        logger.debug("config-report fan-out failed for %s: %s", url, exc)
        return {"url": url, "reachable": False, "error": str(exc)}


async def fanout(urls: Iterable[str]) -> list[dict]:
    """Collect ``/config-report`` from each URL concurrently, deduplicated.

    Several env vars point at the same pod (RAG_TEMPLATE_URL and VECTOR_RAG_V2_URL,
    for one), so without the dedupe the panel would show the same module twice
    and pay for it twice.
    """
    unique = list(dict.fromkeys(u for u in urls if u))
    if not unique:
        return []
    return list(await asyncio.gather(*(_fetch_one(u) for u in unique)))
