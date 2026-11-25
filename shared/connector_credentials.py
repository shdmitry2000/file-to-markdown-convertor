"""Resolve the LLM connector credentials to use for a given space.

**Per-space-only at runtime.** Every space owns its full connector (set at creation
from the env defaults); the ``SpaceConnectorBound`` dependency rejects any request whose
space has no connector model (400 — no silent global fallback). The env
(``PROXY_*`` / ``LLM_CONNECTOR_TYPE`` + ``LLM_TYPE``) is the CREATE-TIME default source
(:func:`services.gateway.routers.connectors.connector_defaults`), NOT a runtime tier.

Resolution is **template > space** (field-by-field, template wins):

* **space**   — the per-space ``space_connectors`` row (control-plane DB).
* **template**— the template's OWN self-contained connector config (no DB), so a
  template runs + is tested completely standalone. Authoritative when set.

:func:`resolve_connector_creds` reads the space row (fails open to the env values so it
never raises); :func:`resolve_llm_connector` layers the template tier on top and is what
the LLM factory lanes call. The evaluator resolves the space's connector too, via
:mod:`shared.space_llm` (pinned clients for its worker-thread / Ragas context).

**Isolation footgun (by design choice):** template-wins applies to creds too, so a
template that sets connector creds overrides EVERY space's per-tenant creds. Prod
template pods MUST leave creds unset; a ``template_overrides_space`` diagnostic warns
at runtime when a template cred shadows a space cred.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, replace
from typing import Any, Optional

from shared import secret_box

logger = logging.getLogger(__name__)

# ``resolve_connector_creds`` runs on EVERY template request (router-level
# ``SpaceConnectorBound`` dependency), so an uncached read costs a DB round trip
# per request — dominated by query *planning*, not execution. Connector rows change
# rarely, so a short TTL removes that hop.
#
# Writers live in the gateway process while readers live in the template processes,
# so `invalidate_connector_cache` only clears the local process. Other processes
# converge on TTL expiry: a connector edit takes up to CONNECTOR_CACHE_TTL_SECONDS
# to take effect everywhere. Set it to 0 to disable caching entirely.
CONNECTOR_CACHE_TTL_SECONDS = float(os.getenv("CONNECTOR_CACHE_TTL_SECONDS", "900"))

# "This space has no connector row" is cached too, but only briefly. Held for the full
# TTL it outlived the fix: a template that looked once before the row existed (or
# while it was being saved) kept answering "no LLM connector configured" for 15
# minutes, because the gateway's invalidation never reaches the template process.
CONNECTOR_NEGATIVE_CACHE_TTL_SECONDS = float(
    os.getenv("CONNECTOR_NEGATIVE_CACHE_TTL_SECONDS", "10"))

# space -> (expires_at_monotonic, row or None).
_connector_cache: dict[str, tuple[float, Optional[dict[str, Any]]]] = {}


def invalidate_connector_cache(space: Optional[str] = None) -> None:
    """Drop cached connector rows for ``space`` (or all spaces when None).

    Process-local — see the note above on cross-process convergence.
    """
    if space is None:
        _connector_cache.clear()
    else:
        _connector_cache.pop(space, None)


@dataclass(frozen=True)
class ConnectorCreds:
    base_url: str
    connector_id: str
    auth_token: str
    source: str  # "template" | "space" | "global" (which tier the identity came from)
    # GCP project + Vertex region. Non-secret; None → inherit the global env.
    # Used by both the vertex_proxy (URL) and vertex_ai (ADC) lanes.
    project: Optional[str] = None
    location: Optional[str] = None
    # Prefixed provider strings (e.g. ``vertex_proxy/gemini-3.5-flash``). Non-secret;
    # None → inherit. ``model`` selects the answer-generation provider; ``embedding_model``
    # selects the retrieval-embedding provider (EMBEDDING_TYPE equivalent).
    model: Optional[str] = None
    embedding_model: Optional[str] = None
    # The space's Context cache switch (llm_factory/core/context_cache.py): the
    # contextualizer caches each document. Space tier only; off unless turned on.
    cache_enabled: bool = False

    def is_complete(self) -> bool:
        return bool(self.base_url and self.connector_id and self.auth_token)


@dataclass(frozen=True)
class TemplateConnectorConfig:
    """The template tier: a template's OWN connector config, loaded self-contained
    (env + a template-local file — no control-plane DB). Any field left None/empty
    falls through to the space, then global, tier."""
    base_url: Optional[str] = None
    connector_id: Optional[str] = None
    auth_token: Optional[str] = None
    project: Optional[str] = None
    location: Optional[str] = None
    model: Optional[str] = None
    embedding_model: Optional[str] = None

    def has_any(self) -> bool:
        return any((self.base_url, self.connector_id, self.auth_token,
                    self.project, self.location, self.model, self.embedding_model))

    def has_identity(self) -> bool:
        """True when the template sets any part of the connector IDENTITY (creds)
        — the fields that can shadow a space's per-tenant creds."""
        return any((self.base_url, self.connector_id, self.auth_token))


def global_connector_creds() -> ConnectorCreds:
    """The global connector credentials + GCP project/location from env."""
    from shared.llm_factory.core import config as _cfg

    return ConnectorCreds(
        base_url=(os.environ.get("PROXY_BASE_URL") or "").rstrip("/"),
        connector_id=os.environ.get("PROXY_CONNECTOR_ID") or "",
        auth_token=os.environ.get("PROXY_AUTH_TOKEN") or "",
        source="global",
        project=_cfg.gcp_project_id_for_vertex_proxy(),
        location=os.environ.get("VERTEX_LOCATION") or None,
        # model/embedding stay None at the global tier: a non-None value here means
        # the space or template *explicitly* chose a model (authoritative). The env
        # default (LLM_CONNECTOR_TYPE + LLM_TYPE / EMBEDDING_TYPE) is the final fallback,
        # applied by the consumers (answer path, embedding client), so it does not
        # shadow a generation-section model.
        model=None,
        embedding_model=None,
    )


@dataclass(frozen=True)
class ConnectorResolution:
    """The connector-identity decision for a space row — the SINGLE branching shared by
    the resolver (:func:`resolve_connector_creds`) and the read-only diagnostics
    (:func:`shared.config_diagnostics.diagnose_connector_creds`), so the two can't drift.
    """
    creds: ConnectorCreds
    code: str    # connector_space | connector_inherited | token_undecryptable | connector_incomplete
    level: str   # "info" | "error"
    #: How the row's token is stored, independent of which connector identity won:
    #: "none" (no token) | "encrypted" (a KEK sealed it) | "plaintext" (no KEK — the value
    #: sits in the clear in a column named auth_token_encrypted). Lives here so diagnostics
    #: can report it by mapping a field instead of re-deriving the KEK branch and drifting.
    token_at_rest: str = "none"


#: Opt-in escape hatch for local/standalone development, where no KEK is delivered.
#: Truthy ⇒ :func:`seal_token` falls back to storing the token in the clear (loudly).
#: Never set this in a deployed environment.
ENV_ALLOW_PLAINTEXT = "ALLOW_UNENCRYPTED_CONNECTOR_TOKENS"


def _plaintext_allowed() -> bool:
    return (os.environ.get(ENV_ALLOW_PLAINTEXT) or "").strip().lower() in ("1", "true", "yes")


def seal_token(space: str, raw_token: str) -> str:
    """Encrypt a connector token for storage. **Refuses to store it in the clear.**

    The write-side counterpart of :func:`classify_connector_row`, and deliberately in the same
    module: the two must agree on what "plaintext without a KEK" means, and splitting them left
    the policy as a private helper in the gateway router while the read semantics lived here.

    Without a KEK this used to pass the token through unencrypted and log a warning. The value
    then landed in a column literally named ``auth_token_encrypted``, so a missing KEK Secret in
    a deployed environment looked exactly like a healthy one — and a warning in a log nobody
    reads is not a control. The write now **fails closed**: no KEK, no write. Setting a
    connector token is an interactive admin action (the space UI), so a 503 telling the operator
    to deliver the KEK is strictly better than a silent plaintext write they never learn about.

    ``ALLOW_UNENCRYPTED_CONNECTOR_TOKENS=true`` restores the old pass-through for local and
    standalone development, which has no KEK to deliver.

    The **read** side stays tolerant on purpose (see :func:`classify_connector_row`): rows
    written in the clear before this change must keep working until they are re-saved.

    Callers must not log the return value — it may be the secret itself.
    """
    if secret_box.is_enabled():
        return secret_box.encrypt(raw_token)
    if _plaintext_allowed():
        logger.warning(
            "%s is not configured and %s is set: storing the connector token for space %r "
            "UNENCRYPTED in auth_token_encrypted. Never do this in a deployed environment.",
            secret_box.ENV_KEK, ENV_ALLOW_PLAINTEXT, space,
        )
        return raw_token
    raise RuntimeError(
        f"{secret_box.ENV_KEK} is not configured, so the connector token for space {space!r} "
        f"cannot be encrypted and will not be stored. Deliver the KEK Secret to this service, "
        f"or set {ENV_ALLOW_PLAINTEXT}=true for local development."
    )


def _mirrors_global(row: dict, glob: ConnectorCreds) -> bool:
    """True when the row's connector identity just equals the global connector — i.e.
    it was seeded/inherited, not an explicit per-space override."""
    return ((row.get("base_url") or "").rstrip("/") == glob.base_url
            and (row.get("connector_id") or "") == glob.connector_id)



def _row_satisfies_scheme(row: dict, model: Optional[str], token: Optional[str]) -> bool:
    """True when the row supplies every credential its provider scheme requires.

    Schemes differ in what "configured" means: vertex_proxy needs base_url +
    connector_id + auth_token, vertex_ai needs project + location and no token at all,
    openai needs only a token. ``credential_columns`` already declares this per scheme —
    the same table the connector router uses to decide which fields to show — so asking
    it keeps one source of truth instead of hard-coding "a token means configured".

    Falls back to the token test if the scheme is unknown, which is the old behaviour.
    """
    try:
        from shared.llm_factory.core.provider_profiles import credential_columns, scheme_of

        required = credential_columns(scheme_of(model or ""))
    except Exception:  # noqa: BLE001 - unknown/unavailable profile: keep the old rule
        return bool(token)
    if not required:
        return False
    for column in required:
        if column == "auth_token":
            if not token:
                return False
        elif not str(row.get(column) or "").strip():
            return False
    return True

def parse_flag(value: Any) -> bool:
    """A stored on/off flag: 'true'/'false' text in every store, a bool from JSON.
    Unset or unreadable → off."""
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in ("1", "true", "on", "yes")


def classify_connector_row(row: dict, glob: ConnectorCreds) -> ConnectorResolution:
    """Resolve a space's connector row against ``glob`` — the one place the token / KEK /
    identity branching lives. Non-secret fields (project / location / model / embedding)
    always apply; the token is plaintext without a KEK, ciphertext with one.
    """
    non_secret = dict(
        project=row.get("project") or glob.project,
        location=row.get("location") or glob.location,
        model=row.get("model") or glob.model,
        embedding_model=row.get("embedding_model") or glob.embedding_model,
        cache_enabled=parse_flag(row.get("cache_enabled")),
    )
    raw = row.get("auth_token_encrypted")
    kek = secret_box.is_enabled()
    # Told apart by what is stored, not by whether this service holds a key, so rows
    # from every version keep working: a sealed (Fernet) token is decrypted — and is
    # unusable without the key, never sent as if it were the token — while a plaintext
    # one (written before tokens were encrypted, or with ALLOW_UNENCRYPTED_CONNECTOR_
    # TOKENS) is used as is, even after a KEK is delivered, until it is saved again.
    sealed = secret_box.looks_sealed(raw)
    at_rest = "none" if not raw else ("encrypted" if sealed else "plaintext")
    if not raw:
        token = None
    elif sealed:
        token = secret_box.try_decrypt(raw) if kek else None
    else:
        token = raw

    # Whether this row IS the space's connector depends on what its scheme needs, not
    # on whether a token happens to be present. vertex_ai authenticates with ADC and
    # requires project + location and NO token, so a token-only test could never
    # classify a vertex_ai space as its own — it fell through to "global" even when
    # fully configured, and SpaceConnectorBound then rejected every request with
    # "space has no LLM connector configured". Observed on three spaces at once.
    if _row_satisfies_scheme(row, non_secret["model"], token):
        base = (row.get("base_url") or "").rstrip("/") or glob.base_url
        connector = row.get("connector_id") or glob.connector_id
        creds = ConnectorCreds(base_url=base, connector_id=connector,
                               auth_token=token or glob.auth_token,
                               source="space", **non_secret)
        return ConnectorResolution(creds, "connector_space", "info", at_rest)
    # No usable token → identity stays global (no half-mix); non-secret fields honored.
    creds = replace(glob, **non_secret)
    if raw and token is None:  # a sealed token this service cannot decrypt (no / other KEK)
        return ConnectorResolution(creds, "token_undecryptable", "error", at_rest)
    if (row.get("base_url") or row.get("connector_id")) and not _mirrors_global(row, glob):
        # An explicit per-space identity (differing from global) with no token is dropped.
        return ConnectorResolution(creds, "connector_incomplete", "error", at_rest)
    return ConnectorResolution(creds, "connector_inherited", "info", at_rest)


def creds_serve_scheme(creds: Optional[ConnectorCreds], scheme: str) -> bool:
    """True when ``creds`` carry a space's (or template's) OWN credentials entered for
    a connection type that authenticates like ``scheme``.

    A space has one token column, entered for the type of its model. Its embedding may
    use another type (an openai model with vertex_proxy embeddings), so a call must
    never take the space's token or proxy identity unless they were entered for that
    call's type — otherwise an OpenAI key goes to the proxy as its Bearer token. Calls
    of any other type use that type's own pod settings."""
    if creds is None or creds.source not in ("space", "template"):
        return False
    from shared.llm_factory.core.provider_profiles import (
        PROVIDER_PROFILES, scheme_of, share_credentials)

    # No or unknown model type → the connector is proxy-shaped, as the UI's type picker
    # (DEFAULT_CONNECTOR_TYPE) and credential_columns() both assume.
    own = scheme_of(creds.model or "")
    if own not in PROVIDER_PROFILES:
        own = "vertex_proxy"
    return share_credentials(own, scheme)


def space_token_for(creds: Optional[ConnectorCreds], scheme: str) -> Optional[str]:
    """The space's own token for a ``scheme`` call, or None → the caller keeps that
    scheme's own pod setting (its env variable)."""
    if creds_serve_scheme(creds, scheme) and creds.auth_token:
        return creds.auth_token
    return None


async def resolve_connector_creds(space: Optional[str]) -> ConnectorCreds:
    """Return the connector credentials for ``space`` (2-tier core: space over global).

    Non-secret fields always apply; the token needs a KEK only to *decrypt* (it is
    stored plaintext without one). Never raises — fails open to global.
    """
    glob = global_connector_creds()
    if not space:
        return glob

    cached = _connector_cache.get(space) if CONNECTOR_CACHE_TTL_SECONDS > 0 else None
    if cached is not None and cached[0] > time.monotonic():
        row = cached[1]
    else:
        try:
            from shared.db.connectors import make_connector_store

            row = await make_connector_store().get_space_connector(space)
        except Exception as e:  # store/DB unavailable — fall back (expected in standalone)
            # WARNING, not DEBUG: the fallback has no model, so every request to this
            # space then fails with "no LLM connector configured" — and at DEBUG the
            # real cause (wrong DSN, missing grant, DB down) was invisible.
            logger.warning(
                "connector resolve: cannot read the connector for space %r (%s: %s) — "
                "falling back to the platform defaults, which name no model",
                space, type(e).__name__, e)
            return glob  # not cached: a transient outage must not pin the fallback
        if CONNECTOR_CACHE_TTL_SECONDS > 0:
            ttl = CONNECTOR_CACHE_TTL_SECONDS if row else min(
                CONNECTOR_CACHE_TTL_SECONDS, CONNECTOR_NEGATIVE_CACHE_TTL_SECONDS)
            _connector_cache[space] = (time.monotonic() + ttl, row)

    if not row:
        return glob
    return classify_connector_row(row, glob).creds


async def resolve_llm_connector(
    space: Optional[str], *, template_cfg: Optional[TemplateConnectorConfig] = None,
) -> ConnectorCreds:
    """3-tier connector resolution: global < space < **template** (template wins).

    Composes the space+global core (:func:`resolve_connector_creds`) with the
    template tier, field-by-field: a field the template sets wins; otherwise it
    falls through to the space, then global, value. The identity fields are the
    exception: they move as one unit (see below). When ``template_cfg`` is None
    it is read from the request-bound template config (set by the template routers),
    so the space tier stays optional and the template resolves standalone.
    """
    if template_cfg is None:
        try:
            from shared.request_context import get_current_template_connector
            template_cfg = get_current_template_connector()
        except Exception:  # pragma: no cover - contextvar not present
            template_cfg = None

    base = await resolve_connector_creds(space)
    if not template_cfg or not template_cfg.has_any():
        return base

    # The identity (base_url + connector_id + auth_token) is ONE unit, never mixed across
    # tiers: merged field by field, a template that set only its base_url sent the
    # space's token to the template's endpoint (review 1.7.1). A template that sets any
    # identity field supplies all three; what it leaves empty comes from the pod's own
    # env (the same operator configured both), never from the space.
    ident = base
    if template_cfg.has_identity():
        glob = global_connector_creds()
        ident = ConnectorCreds(
            base_url=template_cfg.base_url or glob.base_url,
            connector_id=template_cfg.connector_id or glob.connector_id,
            auth_token=template_cfg.auth_token or glob.auth_token,
            source="template")

    return ConnectorCreds(
        base_url=ident.base_url.rstrip("/"),
        connector_id=ident.connector_id,
        auth_token=ident.auth_token,
        source=ident.source,
        project=template_cfg.project or base.project,
        location=template_cfg.location or base.location,
        model=template_cfg.model or base.model,
        embedding_model=template_cfg.embedding_model or base.embedding_model,
        cache_enabled=base.cache_enabled,
    )


_ENV_FOR = {"base_url": "PROXY_BASE_URL", "connector_id": "PROXY_CONNECTOR_ID",
            "auth_token": "PROXY_AUTH_TOKEN", "project": "GOOGLE_CLOUD_PROJECT_ID",
            "location": "VERTEX_LOCATION"}


def missing_credentials(creds: ConnectorCreds, model: Optional[str]) -> list[str]:
    """Credential fields ``model``'s scheme needs that ``creds`` (space values with the
    platform's filled in) still lacks — i.e. set neither on the space nor on the pod."""
    from shared.llm_factory.core.provider_profiles import credential_columns, scheme_of

    required = credential_columns(scheme_of(model or ""))
    return sorted(c for c in required if c != "location" and not str(getattr(creds, c, "") or "").strip())


async def explain_space_connector(space: str) -> dict:
    """Why this service does (or does not) have an LLM connector for ``space``.

    Reads the store directly — no cache — and names the first thing that stops the
    space's connector from being usable *in this process*. The gateway and every
    template run this independently: they can disagree (a template on a different
    database, or without the gateway's CONNECTOR_KEK), and "no LLM connector
    configured" alone could not say which. Never returns the token.
    """
    from shared.llm_factory.core.provider_profiles import credential_columns, scheme_of

    out: dict[str, Any] = {
        "space": space, "kek_enabled": secret_box.is_enabled(), "store": "ok",
        "row_found": False, "model": None, "missing": [], "token_at_rest": "none",
        "code": None, "ok": False, "reason": "",
    }
    try:
        from shared.db.connectors import make_connector_store

        store = make_connector_store()
        out["store_backend"] = type(store).__name__
        row = await store.get_space_connector(space)
    except Exception as e:  # noqa: BLE001 — the failure IS the answer
        out.update(store="error", code="store_unreadable",
                   reason=f"cannot read the connector store: {type(e).__name__}: {str(e)[:300]}")
        return out
    if not row:
        out.update(code="no_row", reason=(
            f"no connector is saved for space {space!r} in the database this service "
            f"reads ({out['store_backend']}) — save it on the space's connector page"))
        return out

    out["row_found"] = True
    res = classify_connector_row(row, global_connector_creds())
    model = row.get("model") or None
    raw = row.get("auth_token_encrypted")
    sealed = secret_box.looks_sealed(raw)
    out.update(model=model, code=res.code,
               token_at_rest=("encrypted" if sealed else "plaintext") if raw else "none")
    required = credential_columns(scheme_of(model or ""))
    # The row's own columns: res.creds carries the platform's env values wherever the
    # row's do not resolve, which would hide a token (or endpoint) the row lacks.
    out["missing"] = sorted(
        c for c in required
        if not str(row.get("auth_token_encrypted" if c == "auth_token" else c) or "").strip())
    if not model:
        out.update(code="no_model", reason="a connector is saved but its model is empty")
    elif sealed and not out["kek_enabled"]:
        out.update(code="token_undecryptable", reason=(
            "the token is stored encrypted, but this service has no CONNECTOR_KEK to "
            "decrypt it — give it the gateway's"))
    elif res.code == "token_undecryptable":
        out["reason"] = ("the token is stored encrypted, but this service's CONNECTOR_KEK "
                         "cannot decrypt it — the key differs from the gateway's")
    elif res.code == "connector_space":
        out.update(ok=True, reason="ok — the space's own connector is used" + (
            " (its token is stored unencrypted, from before a CONNECTOR_KEK was set — "
            "it still works; save the connector again to encrypt it)"
            if raw and not sealed and out["kek_enabled"] else ""))
    elif res.code == "connector_inherited":
        # Blank fields inherit the platform's values by design (vertex_ai with project /
        # location left blank, claude_code with nothing to store) — but only when the
        # platform HAS them. A lane whose pods carry no PROXY_* env leaves a proxy space
        # with no endpoint, which the form's placeholders made look configured.
        unresolved = missing_credentials(res.creds, model)
        if unresolved:
            out.update(code="connector_incomplete", reason=(
                f"missing {', '.join(unresolved)} — blank on the space and not set on this "
                f"platform either ({', '.join(_ENV_FOR[c] for c in unresolved)}): enter "
                "them on the space's connector page"))
        else:
            out.update(ok=True, reason=(
                f"ok — this row leaves {', '.join(out['missing'])} blank, so the platform's "
                "values are used" if out["missing"] else "ok — the platform's connector is used"))
    else:
        out["reason"] = ("the saved connector is incomplete for "
                         f"{scheme_of(model)}: missing {', '.join(out['missing']) or 'credentials'}")
    return out
