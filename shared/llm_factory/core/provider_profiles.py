"""Provider profiles — the declarative "machine types" contract.

One :class:`ProviderProfile` per provider scheme declares which parameters that
scheme needs (required env vars, any-of groups, GCP project, ADC). It is the
single documented place that answers "what does ``openai/`` / ``vertex_ai/`` /
``vertex_proxy/`` actually require to run?" and it backs :func:`validate_provider`,
which the factory calls at build time to fail fast with a clear message instead
of a deep provider stack trace.

Params are still *read* ad-hoc by each backend (LiteLLM reads OPENAI_API_KEY
itself, etc.); this module only validates their presence.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from . import config


@dataclass(frozen=True)
class ProviderProfile:
    scheme: str
    #: env vars that must all be set.
    required_env: tuple[str, ...] = ()
    #: groups where at least one of each group must be set (e.g. GOOGLE_API_KEY | GEMINI_API_KEY).
    any_of_env: tuple[tuple[str, ...], ...] = ()
    #: needs a GCP project (GOOGLE_CLOUD_PROJECT_ID or GOOGLE_CLOUD_PROJECT).
    needs_project: bool = False
    #: needs Application Default Credentials (documented; not machine-checked here).
    needs_adc: bool = False
    #: connector-row columns this scheme actually stores. The space connector row is
    #: universal (one table, all columns); a scheme only owns the subset named here,
    #: and the rest are cleared on seed so a vertex_ai space can't retain stale proxy
    #: credentials. Mirrored by the frontend picker in lib/connectorSchemes.ts.
    credential_columns: tuple[str, ...] = ()
    #: env var holding this scheme's bearer/API token, when it uses one.
    token_env: str | None = None
    #: model-id prefixes this scheme actually serves. Empty = accepts anything
    #: (unknown or self-hosted providers), so this only ever rejects a pairing it
    #: is sure about — a wrong table entry must not block a legitimate model.
    model_prefixes: tuple[str, ...] = ()
    notes: str = ""


_GOOGLE_KEYS = ("GOOGLE_API_KEY", "GEMINI_API_KEY")

PROVIDER_PROFILES: dict[str, ProviderProfile] = {
    "gemini": ProviderProfile(
        "gemini",
        model_prefixes=("gemini-",), any_of_env=(_GOOGLE_KEYS,),
        credential_columns=("auth_token",), token_env="GOOGLE_API_KEY",
        notes="Google AI Studio via LiteLLM.",
    ),
    "google_genai": ProviderProfile(
        "google_genai",
        model_prefixes=("gemini-",), any_of_env=(_GOOGLE_KEYS,),
        credential_columns=("auth_token",), token_env="GOOGLE_API_KEY",
        notes="Native google-genai SDK; same Google keys.",
    ),
    "openai": ProviderProfile(
        "openai",
        model_prefixes=("gpt-", "o1-", "o3-", "o4-", "text-embedding-"), required_env=("OPENAI_API_KEY",),
        credential_columns=("auth_token",), token_env="OPENAI_API_KEY",
    ),
    "anthropic": ProviderProfile(
        "anthropic",
        model_prefixes=("claude-",), required_env=("ANTHROPIC_API_KEY",),
        credential_columns=("auth_token",), token_env="ANTHROPIC_API_KEY",
    ),
    "vertex_proxy": ProviderProfile(
        "vertex_proxy",
        model_prefixes=("gemini-",),
        required_env=(
            config.ENV_PROXY_BASE_URL,
            config.ENV_PROXY_CONNECTOR,
            config.ENV_PROXY_AUTH_TOKEN,
        ),
        credential_columns=("base_url", "connector_id", "auth_token"),
        token_env=config.ENV_PROXY_AUTH_TOKEN,
        notes="Vertex AI format via the Raw Connector platform.",
    ),
    "gemini_proxy": ProviderProfile(
        "gemini_proxy",
        model_prefixes=("gemini-",),
        required_env=(
            config.ENV_PROXY_BASE_URL,
            config.ENV_PROXY_CONNECTOR,
            config.ENV_PROXY_AUTH_TOKEN,
        ),
        credential_columns=("base_url", "connector_id", "auth_token"),
        token_env=config.ENV_PROXY_AUTH_TOKEN,
        notes="Gemini format via the Raw Connector platform.",
    ),
    "vertex_ai": ProviderProfile(
        "vertex_ai", model_prefixes=("gemini-",), needs_project=True, needs_adc=True,
        credential_columns=("project", "location"),
        notes="Vertex AI direct; project/location + ADC.",
    ),
}

#: Columns assumed for a scheme with no profile. Proxy-shaped rather than empty so
#: an unrecognised proxy variant still seeds usable credentials — the same
#: fall-open policy the frontend picker applies via DEFAULT_CONNECTOR_TYPE.
_UNKNOWN_SCHEME_COLUMNS: tuple[str, ...] = ("base_url", "connector_id", "auth_token")


def credential_columns(scheme: str) -> frozenset[str]:
    """Connector-row columns a scheme owns. Unknown schemes fall back to proxy shape."""
    profile = PROVIDER_PROFILES.get(scheme)
    if profile is None:
        return frozenset(_UNKNOWN_SCHEME_COLUMNS)
    return frozenset(profile.credential_columns)


def auth_token_env(scheme: str) -> str:
    """The scheme's token from the environment, or "" when it uses none."""
    profile = PROVIDER_PROFILES.get(scheme)
    env_name = profile.token_env if profile else config.ENV_PROXY_AUTH_TOKEN
    return (os.environ.get(env_name) or "") if env_name else ""


def share_credentials(scheme_a: str, scheme_b: str) -> bool:
    """True when two schemes authenticate with the same parameters — the same token
    variable and the same connector-row columns (vertex_proxy and gemini_proxy share
    the connector; gemini and google_genai share the Google key). A credential entered
    for one scheme is only ever sent to a scheme that shares it."""
    if scheme_a == scheme_b:
        return True
    a, b = PROVIDER_PROFILES.get(scheme_a), PROVIDER_PROFILES.get(scheme_b)
    if a is None or b is None:
        return False
    return (a.token_env, a.credential_columns) == (b.token_env, b.credential_columns)


def scheme_of(provider: str) -> str:
    """The provider scheme (part before the first ``/``)."""
    spec = (provider or "").strip()
    return spec.split("/", 1)[0].strip() if "/" in spec else spec


def _space_supplied(scheme: str, creds) -> tuple[bool, bool]:
    """(token, project) that the space's own connector supplies for ``scheme`` — only
    credentials entered for a type that authenticates like ``scheme`` count."""
    if creds is None:
        try:
            from shared.request_context import get_current_resolved_connector
            creds = get_current_resolved_connector()
        except Exception:  # pragma: no cover - no request context available
            creds = None
    try:
        from shared.connector_credentials import creds_serve_scheme
        own = creds_serve_scheme(creds, scheme)
    except Exception:  # pragma: no cover
        own = False
    if not own:
        return False, False
    return bool(creds.auth_token), bool(creds.project)


def validate_provider(provider: str, creds=None) -> list[str]:
    """Return human-readable messages for any missing params of ``provider``.

    A parameter the space's own connector supplies (``creds``, else the request-bound
    resolved connector) counts as present: a space with its own OpenAI key or GCP
    project runs on a pod that has none. Empty list = OK. Unknown schemes return
    ``[]`` (pass through — LiteLLM or the backend handles them). Never raises.
    """
    scheme = scheme_of(provider)
    profile = PROVIDER_PROFILES.get(scheme)
    if profile is None:
        return []

    has_token, has_project = _space_supplied(scheme, creds)
    problems: list[str] = []
    for env in profile.required_env:
        if has_token and env == profile.token_env:
            continue
        if not (os.environ.get(env) or "").strip():
            problems.append(f"{scheme}: missing required env var {env}")
    for group in profile.any_of_env:
        if has_token and profile.token_env in group:
            continue
        if not any((os.environ.get(e) or "").strip() for e in group):
            problems.append(f"{scheme}: set one of {' or '.join(group)}")
    if profile.needs_project and not has_project and not config.gcp_project_id_for_vertex_proxy():
        problems.append(
            f"{scheme}: missing GCP project "
            f"({config.ENV_GOOGLE_CLOUD_PROJECT_ID} or {config.ENV_GOOGLE_CLOUD_PROJECT})"
        )
    return problems


def model_fits_scheme(scheme: str, model: str) -> bool:
    """True if ``model`` is one this ``scheme`` plausibly serves.

    Prefix-based and deliberately permissive: a scheme with no declared prefixes
    accepts anything, so an incomplete table can never block a legitimate model.
    It exists to catch pairings that are certainly wrong — `openai/gemini-3.5-flash`
    — not to be an allowlist of every model a provider offers.
    """
    profile = PROVIDER_PROFILES.get(scheme)
    if profile is None or not profile.model_prefixes:
        return True
    bare = (model or "").split("/", 1)[-1].strip()
    return any(bare.startswith(prefix) for prefix in profile.model_prefixes)
