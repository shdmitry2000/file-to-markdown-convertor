"""Deployment config report — what env this pod actually received.

The chart delivers env three ways: inline ``env:`` entries, a ConfigMap, and a
Secret, the latter two via ``envFrom``. Nothing in the running process reports
which of them arrived, so a renamed or absent ConfigMap looks exactly like a
healthy pod — the container starts, the code falls back to defaults, and the
first symptom is a wrong answer hours later.

Two things are built on the spec below:

* :func:`require_env` — startup fail-fast. Missing *required* env stops the pod
  with every missing name listed at once, so one restart tells you the whole
  story instead of one name per crash loop.
* :func:`config_report` — the read-only inventory behind the admin config panel.

Alongside them, :func:`require_writable_project_data` covers the other half of a
deployment's contract: env can be complete and correct while the shared volume
the env points at is unwritable by the pod.

Values are reported for non-secret vars. Secrets NEVER have their value
reported. They get ``present`` plus, only when the value is long enough to be
worth hashing, a short fingerprint so two pods can be compared without either
one revealing anything: a truncated digest of a short or low-entropy secret is
brute-forceable offline, which would turn a diagnostics page into an oracle.

The spec is deliberately a curated list, not everything the chart sets. Its job
is the config whose absence breaks the service or silently changes behaviour;
enumerating all forty ingest timeouts would bury exactly that.
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from shared.utils.tls import describe as describe_tls
from shared.utils.tls import install as install_tls

logger = logging.getLogger(__name__)

# A digest of a secret shorter than this is not safe to publish: the search space
# is small enough to invert offline, so the fingerprint would leak the value.
_MIN_FINGERPRINT_LEN = 20


@dataclass(frozen=True)
class EnvSpec:
    name: str
    required: bool = False
    secret: bool = False
    note: str = ""


@dataclass(frozen=True)
class OneOf:
    """At least one of ``names`` must be set, else required config is missing."""
    names: tuple[str, ...]
    note: str = ""


@dataclass(frozen=True)
class ModuleSpec:
    env: tuple[EnvSpec, ...] = ()
    one_of: tuple[OneOf, ...] = ()


# Every module that opens the platform database.
_DATABASE = (
    # The one non-credential var that must be explicit. Callers disagree on a
    # default when it is absent — metadata_router assumes "oracle", the agent
    # assumes "postgresql" — so an unset value does not mean one consistent
    # fallback, it means different components pick different databases.
    EnvSpec("RAG_BACKEND", required=True,
            note="selects the Store implementation; callers disagree on a default"),
    EnvSpec("ORACLE_MAIN_SCHEMA",
            note="Oracle only: the schema that owns the control plane, as the DBA "
                 "named it. Default RAGP_DEV_DATA; must match their scripts"),
    EnvSpec("ORACLE_SPACE_PREFIX",
            note="Oracle only: every space user starts with this. Default "
                 "RAGP_SPACE_; must equal the prefix in the DBA's "
                 "CREATE_SPACE_USER, or every space creation fails ORA-20000"),
    EnvSpec("SPACE_PROVISION_PROCEDURE",
            note="Oracle only: who owns the DBA's space procedures. Default "
                 "SYSTEM; set it only if your DBA kept them elsewhere. Spaces "
                 "are created by those procedures and by nothing else"),
    EnvSpec("CONNECTOR_KEK", secret=True,
            note="unset means per-space connector tokens sit unencrypted at rest"),
)

# Modules that never open the database: the converter receives files and a
# forwarded connector, so RAG_BACKEND and the backend's credentials are not its
# config and must not be reported missing on its pod.
_NO_DATABASE = frozenset({"markdown-api"})

# Every module, whatever its role.
_BASE = (
    EnvSpec("INTERNAL_SERVICE_TOKEN", secret=True,
            note="unset disables service-to-service verification"),
    EnvSpec("TRACING_BACKEND",
            note="none | otel | dynatrace; unset = otel if OTEL_EXPORTER_OTLP_ENDPOINT set"),
    EnvSpec("OTEL_EXPORTER_OTLP_ENDPOINT",
            note="otel backend; see /health/telemetry for effective state"),
    EnvSpec("DYNATRACE_OTLP_ENDPOINT", note="dynatrace backend: collector or OTLP API"),
    EnvSpec("DYNATRACE_API_TOKEN", secret=True, note="dynatrace backend; optional"),
    EnvSpec("LLM_CONNECTOR_TYPE", note="answer model's connection type"),
    EnvSpec("LLM_TYPE", note="answer model name"),
    EnvSpec("EMBEDDING_TYPE", note="embedding model name, on LLM_CONNECTOR_TYPE"),
    EnvSpec("LLM_TEMPERATURE", note="default 1.0; always sent"),
    EnvSpec("LLM_MAX_TOKENS", note="default 8192"),
    EnvSpec("LLM_REQUEST_TIMEOUT_SECONDS", note="default 300"),
    EnvSpec("LLM_QUERY_TIMEOUT_SECONDS", note="default 120; one LLM call on a user request"),
    EnvSpec("HOP_DELTA_SECONDS", note="default 10; a caller's margin over its callee"),
    EnvSpec("HTTP_TIMEOUT_SECONDS", note="default 30; one plain call between services"),
    EnvSpec("STREAM_KEEPALIVE_SECONDS", note="default 15; keepalive frame on a quiet answer stream"),
    EnvSpec("DB_TIMEOUT_SECONDS", note="default 15; pool wait, and a statement inside a user request"),
    EnvSpec("LLM_MAX_CONCURRENCY", note="default 8; calls in flight per process"),
    EnvSpec("PROJECTS_BASE_PATH"),
)

# Required env per RAG_BACKEND. Only the selected backend's block is enforced —
# a postgres deployment must not be failed for having no ORACLE_DSN.
_BACKEND_SPECS: dict[str, ModuleSpec] = {
    "postgres": ModuleSpec(
        env=(
            EnvSpec("POSTGRES_HOST", required=True),
            EnvSpec("POSTGRES_PORT"),
            EnvSpec("POSTGRES_USER", required=True),
            EnvSpec("POSTGRES_PASSWORD", required=True, secret=True),
        ),
        one_of=(OneOf(("POSTGRES_DATABASE", "POSTGRES_DB"),
                      "the database name, under either accepted spelling"),),
    ),
    "mongodb": ModuleSpec(
        env=(
            EnvSpec("MONGODB_DATABASE"),
            EnvSpec("MONGODB_USER"),
            EnvSpec("MONGODB_PASSWORD", secret=True),
        ),
        # The client prefers MONGODB_URI and falls back to assembling from
        # components, so requiring the URI outright would fail a working cluster.
        one_of=(OneOf(("MONGODB_URI", "MONGODB_HOST"),
                      "canonical URI, or the host for the components fallback"),),
    ),
    "oracle": ModuleSpec(
        env=(
            EnvSpec("ORACLE_DSN", required=True, note="Easy Connect: host:port/service"),
            EnvSpec("ORACLE_USER", required=True),
            EnvSpec("ORACLE_PASSWORD", required=True, secret=True),
        ),
    ),
}

# Per-module additions on top of _BASE.
_MODULE_SPECS: dict[str, ModuleSpec] = {
    "gateway": ModuleSpec(env=(
        EnvSpec("CORS_ORIGINS", note="wildcard here disables credentialed CORS"),
        EnvSpec("AUTH_CACHE_TTL_SECONDS", note="0/unset re-pays a bcrypt verify per request"),
        # Reported, not required: the code falls back to a localhost default, so
        # demanding them would crash every local run to catch a cluster-only
        # mistake. Absent in the report is the signal; a crash would be a lie.
        EnvSpec("MARKDOWN_API_URL", note="unset falls back to http://localhost:7001"),
        EnvSpec("EVALUATOR_SERVICE_URL"),
        # Sign-in and access. Every one changes who gets in or with what, silently.
        EnvSpec("AUTH_MODE", note="internal (default) | both | oauth: passwords, IdP, or IdP only"),
        EnvSpec("AUTHZ_MODE", note="hybrid (default) | ad_only: space roles only from the space ledger"),
        EnvSpec("AUTH_OIDC_NAME", note="sign-in button label"),
        EnvSpec("AUTH_OIDC_ISSUER", note="IdP issuer; the ID token's iss must equal it"),
        EnvSpec("AUTH_OIDC_DISCOVERY_URL", note="unset = <issuer>/.well-known/openid-configuration"),
        EnvSpec("AUTH_OIDC_CLIENT_ID", note="the ID token's audience"),
        EnvSpec("AUTH_OIDC_ID_CLAIM", note="default sub; becomes the user id and the ledger's user:<id>"),
        EnvSpec("AUTH_OIDC_GROUPS_CLAIM", note="default groups"),
        EnvSpec("AUTH_OIDC_ADMIN_GROUP", note="comma list; members are global admins for that session; more groups on Administration → SSO"),
        EnvSpec("AUTH_OIDC_SECURITY_ADMIN_GROUP", note="comma list; members are security admins; no hand grants while any is set; more on Administration → SSO"),
        EnvSpec("AUTH_OIDC_USER_GROUP", note="comma list; the front door: only members sign in; unset (here and on Administration → SSO) = anyone the IdP signs in"),
        EnvSpec("AUTH_OIDC_SIGNING_ALGORITHMS", note="default RS256"),
        EnvSpec("AUTH_OIDC_ALLOW_HTTP", note="true allows an http issuer: lab only"),
        EnvSpec("AUTH_OIDC_CERTIFICATE_SELFSIGNED", note="default true: accepts the IdP's self-signed certificate (IdP calls only); false verifies it"),
        EnvSpec("NODE_TLS_REJECT_UNAUTHORIZED", note="0 = no IdP certificate check (recorded exception for lab lanes)"),
        EnvSpec("NODE_EXTRA_CA_CERTS", note="PEM of extra CAs trusted for the IdP, added to the public ones"),
        EnvSpec("API_TOKEN_DEFAULT_EXPIRY_DAYS", note="0/unset = no default end date; in ad_only every key must then be given one"),
        EnvSpec("AUTH_PASSWORD_LOGIN_ENABLED", note="unset = on; false turns password sign-in off (AUTH_MODE=oauth always does)"),
        EnvSpec("AUTH_LOGIN_ATTEMPTS_PER_MINUTE", note="default 10"),
        EnvSpec("SESSION_COOKIE_SECURE", note="false only for plain-http local runs"),
        EnvSpec("SESSION_COOKIE_MAX_AGE_SECONDS"),
        EnvSpec("SESSION_IDLE_TIMEOUT_SECONDS", note="0/unset = no idle timeout"),
        EnvSpec("SESSION_REVOKE_ON_PASSWORD_CHANGE"),
        EnvSpec("SSO_ADMIN_SESSION_MAX_SECONDS", note="default 3600: admin sessions from the IdP"),
        EnvSpec("BOOTSTRAP_ADMIN_PASSWORD", secret=True, note="first-start admin; unused once the admin exists"),
        # The legacy header lane (an auth proxy in front sets the user header).
        EnvSpec("SSO_LOGIN_ENABLED", note="legacy header lane: Sign in with AD"),
        EnvSpec("SSO_LOGIN_LABEL"),
        EnvSpec("AUTH_BACKEND", note="default internal"),
        EnvSpec("AUTH_CONNECTOR_USER_HEADER", note="default x-forwarded-user"),
        EnvSpec("AUTH_CONNECTOR_IDP"),
        EnvSpec("AUTH_CONNECTOR_ADMIN_GROUPS"),
        EnvSpec("AUTH_CONNECTOR_JIT"),
    )),
    "evaluator": ModuleSpec(env=(
        EnvSpec("CORS_ORIGINS"),
    )),
    "agent": ModuleSpec(env=(
        EnvSpec("RAG_SERVICE_URL", note="unset falls back to http://localhost:8002"),
    )),
}

# Template/service URLs. Reported with values (they are addresses, not secrets)
# because "which module is this pod actually pointed at" is the other half of
# the same question — a correct env table on a pod aimed at a dead URL still fails.
_MODULE_URL_VARS = (
    "RAG_SERVICE_URL",
    "EVALUATOR_SERVICE_URL",
    "MARKDOWN_API_URL",
    "RAG_TEMPLATE_URL",
    "VECTOR_RAG_V2_URL",
    "VECTOR_MD_RETRIEVAL_URL",
    "LIGHTRAG_URL",
    "WIKI_INDEX_URL",
)
# Per-template addresses (RAG_TEMPLATE_URL_RAG_TEMPLATES_VECTORLESS, ...): the only
# way the gateway reaches some templates, so without them those never get reported.
_MODULE_URL_PREFIX = "RAG_TEMPLATE_URL_"


class MissingRequiredEnv(RuntimeError):
    """Required env is absent. Raised at startup so the pod stops, loudly."""


def _backend() -> str:
    return (os.getenv("RAG_BACKEND") or "").strip().lower()


def _specs_for(module: str) -> tuple[tuple[EnvSpec, ...], tuple[OneOf, ...]]:
    """The effective spec for *module*: base + backend block + module block."""
    env: list[EnvSpec] = list(_BASE)
    one_of: list[OneOf] = []

    if module not in _NO_DATABASE:
        env.extend(_DATABASE)
        backend = _BACKEND_SPECS.get(_backend())
        if backend:
            env.extend(backend.env)
            one_of.extend(backend.one_of)

    extra = _MODULE_SPECS.get(module)
    if extra:
        env.extend(extra.env)
        one_of.extend(extra.one_of)

    # De-duplicate by name, keeping the strictest requirement seen.
    merged: dict[str, EnvSpec] = {}
    for spec in env:
        prev = merged.get(spec.name)
        if prev is None:
            merged[spec.name] = spec
        elif spec.required and not prev.required:
            merged[spec.name] = spec
    return tuple(merged.values()), tuple(one_of)


def _is_set(name: str) -> bool:
    return bool((os.getenv(name) or "").strip())


def missing_required(module: str) -> list[str]:
    """Names of required env that is absent, including unsatisfied one-of groups."""
    env, one_of = _specs_for(module)
    missing = [s.name for s in env if s.required and not _is_set(s.name)]
    missing.extend(
        " | ".join(g.names) for g in one_of if not any(_is_set(n) for n in g.names)
    )
    return missing


def require_env(module: str) -> None:
    """Fail fast when required env is missing.

    Reports *every* missing name in one message. Discovering them one restart at
    a time is what makes a misconfigured deployment take an afternoon.
    """
    missing = missing_required(module)
    if missing:
        raise MissingRequiredEnv(
            f"{module}: required environment variables are missing or empty: "
            f"{', '.join(missing)}. "
            f"(RAG_BACKEND={_backend() or 'unset'}.) These arrive from the chart's "
            f"ConfigMap and Secret via envFrom — an absent or renamed ConfigMap "
            f"delivers none of them."
        )


class ProjectDataNotWritable(RuntimeError):
    """The shared project-data volume is not writable. Raised at startup."""


def _volume_diagnostics(path: Path) -> str:
    """Who we are, and who owns the deepest directory that actually exists.

    Both halves are needed to act: EACCES on a volume is always a mismatch
    between the two, and neither number alone says which side is wrong.
    """
    parts = [f"running as uid={os.getuid()} gid={os.getgid()}"]
    try:
        parts.append(f"groups={sorted(os.getgroups())}")
    except OSError:
        pass

    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        st = probe.stat()
        parts.append(
            f"{probe} is owned by uid={st.st_uid} gid={st.st_gid} "
            f"mode={oct(st.st_mode & 0o7777)}"
        )
    except OSError as e:
        parts.append(f"cannot stat {probe}: {e}")
    if probe != path:
        parts.append(f"{path} does not exist")
    return "; ".join(parts)


def require_writable_project_data() -> None:
    """Fail fast when the shared project-data volume cannot be written.

    Every ingest artefact — uploads, converted markdown, manifests, the
    conversion cache — lands under PROJECTS_BASE_PATH on a volume this pod does
    not own. When the directory's group does not match the pod's, nothing fails
    until the first upload, and it fails as an EACCES deep inside a conversion
    with no hint that the volume is the cause. The common trigger is a subPath
    into a pre-provisioned claim: kubelet creates a missing subPath directory
    root-owned and 0755, while OpenShift's restricted-v2 runs the pod as a random
    uid with gid 0, so the pod can read the volume and write nothing to it.

    Skipped when PROJECTS_BASE_PATH is unset: that means no chart configured a
    volume, i.e. a local run writing under the repo, not a deployment.
    """
    configured = (os.getenv("PROJECTS_BASE_PATH") or "").strip()
    if not configured:
        return

    base = Path(configured)
    remedy = (
        f"The volume mounted at {base} must be writable by this pod. On a claim "
        f"provisioned by someone else, have the directory created group-writable "
        f"(chmod 2775) and group-owned by a gid the pod runs with, or set "
        f"podSecurityContext.fsGroup to one — the namespace's allowed range is in "
        f"its openshift.io/sa.scc.supplemental-groups annotation."
    )
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise ProjectDataNotWritable(
            f"PROJECTS_BASE_PATH={base} cannot be created: {e}. "
            f"({_volume_diagnostics(base)}.) {remedy}"
        ) from e

    probe = base / f".write-probe-{os.getpid()}"
    try:
        probe.write_text("ok")
    except OSError as e:
        raise ProjectDataNotWritable(
            f"PROJECTS_BASE_PATH={base} exists but is not writable: {e}. "
            f"({_volume_diagnostics(base)}.) {remedy}"
        ) from e
    finally:
        try:
            probe.unlink()
        except OSError:
            # Written but not removable — a directory we can add to and not
            # clean up is still broken enough to name, but not worth failing on.
            pass


def check_project_storage() -> None:
    """Both project-storage boot checks, in one place so they cannot disagree.

    They already did once. ``require_writable_project_data`` skips when
    PROJECTS_BASE_PATH is unset — that means no chart configured a volume, i.e. a
    local run writing under the repo rather than a deployment. ``preflight`` did
    not skip, so adding it made ``uv run`` unable to start the gateway at all, and
    nothing caught it because no test boots this.

    Together they are one question with two halves: is the place this service
    stores documents actually usable? ``require_writable_project_data`` answers it
    for a mounted directory, with the better remedy text for the OCP
    random-uid/gid-0 case; ``preflight`` answers it for whichever backend is
    configured, including an object store the other check knows nothing about.

    Anything added here must keep the shared rule: an unconfigured local run is not
    a failure.
    """
    from shared.file_storage import preflight

    require_writable_project_data()
    # After credentials resolve, so a wrong bucket, an untrusted CA, or path-style
    # addressing against a vhost-only endpoint fails here rather than on someone's
    # first upload.
    logger.info(preflight())
    # Next to storage because it is the other thing that silently breaks a
    # deployment behind a corporate CA (shared/utils/tls.py).
    install_tls()


def project_data_probe() -> tuple[str, str]:
    """``(status, message)`` for the project-data volume — the ``/health``
    counterpart of :func:`require_writable_project_data`.

    Read-only and O(1), because this runs on every readiness poll: the
    authoritative write probe belongs at startup, where failing costs one restart
    rather than a file per ten seconds for the life of the pod. ``os.access``
    still catches both ways a healthy boot turns unwritable later — a gid that
    stops matching after the claim is re-provisioned, and a volume remounted
    read-only.

    ``"unconfigured"`` when PROJECTS_BASE_PATH is unset, for the reason the
    startup check skips: no chart configured a volume, so there is nothing to
    report on and a local run must not read as broken.
    """
    configured = (os.getenv("PROJECTS_BASE_PATH") or "").strip()
    if not configured:
        return "unconfigured", "PROJECTS_BASE_PATH is unset — no volume configured"

    base = Path(configured)
    if not base.is_dir():
        return "unhealthy", (
            f"PROJECTS_BASE_PATH={base} does not exist. ({_volume_diagnostics(base)}.)"
        )
    # X_OK as well as W_OK: a directory that cannot be traversed is unusable even
    # when its mode says writable, and that is the shape a wrong gid takes.
    if not os.access(base, os.W_OK | os.X_OK):
        return "unhealthy", (
            f"PROJECTS_BASE_PATH={base} is not writable by this pod. "
            f"({_volume_diagnostics(base)}.)"
        )
    return "healthy", f"{base} is writable"


def _fingerprint(value: str) -> Optional[str]:
    if len(value) < _MIN_FINGERPRINT_LEN:
        return None
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()[:8]


def _row(spec: EnvSpec) -> dict:
    raw = os.getenv(spec.name)
    value = (raw or "").strip()
    row: dict = {
        "name": spec.name,
        "present": bool(value),
        "required": spec.required,
        "secret": spec.secret,
    }
    if spec.note:
        row["note"] = spec.note
    if not value:
        return row
    if spec.secret:
        fp = _fingerprint(value)
        if fp:
            row["fingerprint"] = fp
    else:
        row["value"] = value
    return row


def deployment_stamp(version: str | None = None) -> dict:
    """Who deployed this pod, from what chart, with which config.

    ``config_checksum`` hashes the values that render the ConfigMap. It is the
    only field that speaks to *freshness* — presence of a var says nothing about
    whether it is current. A pod reports the checksum it started with, so pods
    disagreeing means some are stale; whether any is current is answered by
    comparing against ``helm template``, since the pod cannot read the live
    ConfigMap without cluster API access.
    """
    return {
        # The version built into the image (the app's own, stamped by
        # scripts/set_version.py), so it needs nothing from the chart.
        "version": version,
        "image": os.getenv("APP_IMAGE") or None,
        # The workload's own chart, stamped on the Deployment.
        "release": os.getenv("DEPLOY_RELEASE") or None,
        "chart_version": os.getenv("DEPLOY_CHART_VERSION") or None,
        # The config's provenance, carried by the ConfigMap itself. Differing from
        # the pair above means this service is running another release's config.
        "config_release": os.getenv("CONFIG_RELEASE") or None,
        "config_chart_version": os.getenv("CONFIG_CHART_VERSION") or None,
        "environment": os.getenv("DEPLOY_ENV") or None,
        "config_checksum": os.getenv("CONFIG_CHECKSUM") or None,
        # With several replicas each call lands on one random pod; naming it makes
        # divergence between them visible instead of looking like a flaky report.
        "pod": os.getenv("POD_NAME") or None,
        "namespace": os.getenv("POD_NAMESPACE") or None,
    }


def config_report(module: str, version: str | None = None) -> dict:
    """Read-only inventory of this pod's config. Never contains a secret value."""
    env, one_of = _specs_for(module)
    # One-of members are reported as their own groups rather than folded into the
    # env table: shown there they would each read as an independently missing var,
    # when only the group as a whole has to be satisfied.
    groups = [
        {
            "names": list(g.names),
            "satisfied": any(_is_set(n) for n in g.names),
            "present": [n for n in g.names if _is_set(n)],
            "note": g.note,
        }
        for g in one_of
    ]
    return {
        "module": module,
        "backend": _backend() or None,
        "uses_database": module not in _NO_DATABASE,
        "deployment": deployment_stamp(version),
        "env": [_row(s) for s in sorted(env, key=lambda s: s.name)],
        "one_of": groups,
        "missing_required": missing_required(module),
        "module_urls": {
            n: os.getenv(n)
            for n in (*_MODULE_URL_VARS, *sorted(k for k in os.environ if k.startswith(_MODULE_URL_PREFIX)))
            if _is_set(n)
        },
        "tls": describe_tls(),
    }
