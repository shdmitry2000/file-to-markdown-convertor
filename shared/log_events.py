"""Security events: one JSON line per event, the line Splunk alerts on.

Every management action, data operation and read the security team wants to see is
written through :func:`log_event` with a stable ``event`` name from :data:`CATALOGUE`.
Alerts match on ``event`` (never on message text), so messages can change freely and
event names cannot: renaming one is a breaking change for the security team.

Settings (env):
    LOG_EVENT_SCHEMA   native (default) | cim — cim adds the Splunk Common Information
                       Model names (user, src, action, status, object, ...) to each line.
    LOG_READ_EVENTS    true (default) | false — the read events (search, answer, file
                       download/inspect, question list) on or off.
    AUDIT_LOG_BODY_CUT true (default) | false, AUDIT_LOG_BODY_BYTES 1024 — long string
                       values in an event (a query, a prompt) are cut to that many bytes.

Secrets never reach a line: any field whose NAME looks secret (see :func:`is_secret_key`)
is written as ``***``, at any depth, before anything else happens. Never raises.
"""

from __future__ import annotations

import logging
import os
import re
from contextvars import ContextVar
from typing import Any, Iterable, Optional

logger = logging.getLogger("security.events")

# Who is acting, and from where, for the current request: set once by the gateway's
# auth middleware so a route writes an event without passing the user through.
_actor: ContextVar[Optional[str]] = ContextVar("event_actor", default=None)
_client: ContextVar[Optional[dict]] = ContextVar("event_client", default=None)


def set_event_actor(actor: Optional[str], *, client_ip: Optional[str] = None,
                    forwarded_for: Optional[str] = None) -> None:
    _actor.set(actor)
    _client.set({"client_ip": client_ip, "forwarded_for": forwarded_for})

W, I, E = logging.WARNING, logging.INFO, logging.ERROR

#: event -> (category, level). The contract with the security team: a test fails if
#: code emits a name that is not here, or if an entry here has no emitter.
CATALOGUE: dict[str, tuple[str, int]] = {
    # A. authentication
    "auth.login_succeeded": ("auth", W),
    "auth.login_failed": ("auth", W),
    "auth.logout": ("auth", W),
    "auth.session_revoked": ("auth", W),
    "auth.password_changed": ("auth", W),
    "auth.sso_login_succeeded": ("auth", W),
    "auth.sso_login_failed": ("auth", W),
    # B. users, roles, tokens, SSO mapping
    "user.created": ("user", W),
    "user.admin_created": ("user", W),
    "user.admin_forced": ("user", W),
    "user.profile_changed": ("user", W),
    "user.password_set_by_admin": ("user", W),
    "user.disabled": ("user", W),
    "user.enabled": ("user", W),
    "user.security_admin_granted": ("user", W),
    "user.security_admin_revoked": ("user", W),
    "role.granted": ("user", W),
    "role.revoked": ("user", W),
    "role.space_admin_granted": ("user", W),
    "role.space_admin_removed": ("user", W),
    "token.issued": ("user", W),
    "token.revoked": ("user", W),
    "access.reviewed": ("user", W),
    "access.denied": ("user", W),
    "sso.group_mapped": ("user", W),
    "sso.group_mapped_to_admin": ("user", W),
    "sso.group_unmapped": ("user", W),
    "sso.identity_linked": ("user", W),
    "sso.platform_groups_changed": ("user", W),
    # C. spaces
    "space.created": ("space", W),
    "space.deleted": ("space", W),
    "space_request.submitted": ("space", W),
    "space_request.approved": ("space", W),
    "space_request.refused": ("space", W),
    "space_request.done": ("space", W),
    "space.updated": ("space", W),
    "space.active_system_changed": ("space", W),
    "space.connector_changed": ("space", W),
    "space.connector_removed": ("space", W),
    "space.storage_repaired": ("space", W),
    "space.audit_enabled": ("audit", W),
    "space.audit_disabled": ("audit", W),
    "system.deleted": ("space", W),
    "system.copied": ("space", W),
    "system.updated": ("space", W),
    "system.data_deleted": ("data", W),
    # D. template configuration
    "template.settings_changed": ("config", W),
    "template.config_changed": ("config", W),
    "template.connector_changed": ("config", W),
    "template.topic_changed": ("config", W),
    "template.registered": ("config", W),
    "template.unregistered": ("config", W),
    "platform.settings_changed": ("config", W),
    # E. import / export
    "config.exported": ("transfer", W),
    "config.imported": ("transfer", W),
    "config.imported_as_new_system": ("transfer", W),
    "config.import_rejected": ("transfer", W),
    "questions.exported": ("transfer", W),
    "questions.imported": ("transfer", W),
    "questions.generated": ("data", W),
    "questions.changed": ("data", W),
    "questions.deleted": ("data", W),
    "eval.data_deleted": ("data", W),
    "eval.results_deleted": ("data", W),
    "labels.saved": ("data", W),
    "labels.deleted": ("data", W),
    # F. audit control
    "audit.purged": ("audit", W),
    "audit.enabled": ("audit", I),
    "audit.disabled": ("audit", W),
    "audit.replayed": ("audit", W),
    # G. data operations
    "files.uploaded": ("data", W),
    "files.deleted": ("data", W),
    "ingest.started": ("data", W),
    "ingest.cancelled": ("data", W),
    "index.file_removed": ("data", W),
    "ingest.run_finished": ("data", I),
    "ingest.file_failed": ("data", E),
    "ingest.batch_failed": ("data", E),
    "memory.captured": ("data", W),
    "memory.updated": ("data", W),
    "memory.forgotten": ("data", W),
    "memory.graph_rebuilt": ("data", W),
    "eval.run_started": ("data", W),
    # H. reads (LOG_READ_EVENTS)
    "search.performed": ("read", I),
    "answer.generated": ("read", I),
    "file.downloaded": ("read", I),
    "file.inspected": ("read", I),
    "questions.listed": ("read", I),
}

_SECRET_LAST = {"token", "secret", "password", "passwd", "pwd", "credential", "credentials",
                "dsn", "authorization", "cookie", "kek", "apikey", "passphrase"}
_SECRET_KEY_BEFORE = {"api", "access", "private", "secret", "encryption", "signing", "x"}


def _parts(name: str) -> list[str]:
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(name))
    return [p for p in re.split(r"[^A-Za-z0-9]+", name.lower()) if p]


def is_secret_key(name: str) -> bool:
    """True when a field NAME says its value is a secret (``auth_token``, ``x-api-key``,
    ``oracle_password``, ``embeddingApiKey``). ``max_tokens`` and ``token_id`` are not."""
    parts = _parts(name)
    if not parts:
        return False
    last = parts[-1]
    if last in _SECRET_LAST:
        return True
    if last == "key":
        return len(parts) == 1 or parts[-2] in _SECRET_KEY_BEFORE
    return False


def mask_secrets(value: Any, _depth: int = 0) -> Any:
    """Copy of ``value`` with every secret-named field, at any depth, as ``***``."""
    if _depth > 20:
        return "…"
    if isinstance(value, dict):
        return {k: ("***" if is_secret_key(k) and v not in (None, "") else mask_secrets(v, _depth + 1))
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [mask_secrets(v, _depth + 1) for v in value]
    return value


#: Values a global admin set in the UI (shared.logging_runtime), by env var name.
#: They win over the env var, which stays the default.
_overrides: dict[str, str] = {}


def set_overrides(values: dict[str, str]) -> None:
    global _overrides
    _overrides = dict(values)


def _env(name: str) -> Optional[str]:
    return _overrides.get(name, os.getenv(name))


def _truthy(name: str, default: bool) -> bool:
    raw = _env(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def value_cut_bytes() -> Optional[int]:
    """Bytes a long value is cut to, or None when cutting is switched off."""
    if not _truthy("AUDIT_LOG_BODY_CUT", True):
        return None
    try:
        return max(0, int(_env("AUDIT_LOG_BODY_BYTES") or "1024"))
    except ValueError:
        return 1024


def cut(value: Any, limit: Optional[int] = None) -> Any:
    """A string longer than the cut size, shortened, with the original length noted."""
    limit = value_cut_bytes() if limit is None else limit
    if limit is None or not isinstance(value, str):
        return value
    raw = value.encode("utf-8")
    if len(raw) <= limit:
        return value
    return raw[:limit].decode("utf-8", "ignore") + f"…[{len(raw)} bytes]"


def _cut_all(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _cut_all(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_cut_all(v) for v in value]
    return cut(value)


def changes_between(old: Any, new: Any, prefix: str = "") -> list[dict]:
    """Leaf-level differences as ``[{"field", "old", "new"}]``; a missing side is None.

    Walks into added/removed sections too, so a secret inside one is masked by its name."""
    if isinstance(old, dict) or isinstance(new, dict):
        if not isinstance(old, dict) and old is not None:
            return [{"field": prefix, "old": old, "new": new}]
        if not isinstance(new, dict) and new is not None:
            return [{"field": prefix, "old": old, "new": new}]
        old, new = old or {}, new or {}
        out: list[dict] = []
        for key in sorted(set(old) | set(new), key=str):
            path = f"{prefix}.{key}" if prefix else str(key)
            out += changes_between(old.get(key), new.get(key), path)
        return out
    if old == new:
        return []
    return [{"field": prefix, "old": old, "new": new}]


def _mask_changes(changes: Iterable[dict]) -> list[dict]:
    out = []
    for ch in changes:
        field = str(ch.get("field", ""))
        if any(is_secret_key(p) for p in field.split(".")):
            out.append({"field": field,
                        "old": None if ch.get("old") in (None, "") else "***",
                        "new": None if ch.get("new") in (None, "") else "***"})
        else:
            out.append({"field": field, "old": mask_secrets(ch.get("old")),
                        "new": mask_secrets(ch.get("new"))})
    return out


def _cim_action(event: str, outcome: str) -> str:
    if event.startswith("auth."):
        return "success" if outcome == "success" else "failure"
    if outcome == "denied":
        return "blocked"
    verb = event.rsplit(".", 1)[-1]
    if verb.endswith(("created", "granted", "issued", "uploaded", "imported", "registered",
                      "captured", "mapped", "linked", "started", "enabled", "imported_as_new_system")):
        return "created"
    if verb.endswith(("deleted", "removed", "revoked", "unregistered", "forgotten", "purged",
                      "unmapped", "disabled")):
        return "deleted"
    if verb in ("performed", "generated", "downloaded", "inspected", "listed", "exported"):
        return "read"
    return "modified"


def _cim_fields(event: str, category: str, outcome: str, line: dict) -> dict:
    return {
        "user": line.get("actor"),
        "src": line.get("client_ip"),
        "action": _cim_action(event, outcome),
        "status": "success" if outcome == "success" else "failure",
        "object": line.get("target_id"),
        "object_category": line.get("target_type"),
        "object_id": line.get("target_id"),
        "change_type": category,
        "app": line.get("service"),
        "vendor_product": "RAG platform",
    }


def read_events_enabled() -> bool:
    return _truthy("LOG_READ_EVENTS", True)


def log_event(event: str, *, outcome: str = "success", actor: Optional[str] = None,
              target_type: Optional[str] = None, target_id: Any = None,
              space: Any = None, system_id: Optional[str] = None,
              changes: Optional[Iterable[dict]] = None, level: Optional[int] = None,
              **fields: Any) -> None:
    """Write one security event. Never raises: an event must not fail the action."""
    try:
        category, default_level = CATALOGUE.get(event, ("other", W))
        if category == "read" and not read_events_enabled():
            return
        if level is None:
            level = default_level if outcome == "success" else max(default_level, W)
        line: dict[str, Any] = {"event": event, "category": category, "outcome": outcome}
        if actor is None:
            actor = _actor.get()
        for key, val in (_client.get() or {}).items():
            if val is not None and key not in fields:
                fields[key] = val
        for key, val in (("actor", actor), ("target_type", target_type),
                         ("target_id", target_id), ("space", space), ("system_id", system_id)):
            if val is not None:
                line[key] = str(val) if key in ("target_id", "space") else val
        if changes is not None:
            line["changes"] = _cut_all(_mask_changes(changes))
        line.update(_cut_all(mask_secrets(fields)))
        line["service"] = os.getenv("SERVICE_NAME") or None
        if (_env("LOG_EVENT_SCHEMA") or "native").strip().lower() == "cim":
            line.update({k: v for k, v in _cim_fields(event, category, outcome, line).items()
                         if v is not None})
        if line["service"] is None:
            del line["service"]  # the formatter's own `service` stays
        # The fields also go into the message: LOG_FORMAT=text drops structured fields.
        brief = " ".join(f"{k}={line[k]}" for k in ("outcome", "actor", "target_type",
                                                     "target_id", "space", "system_id")
                         if k in line)
        record = logger.makeRecord(logger.name, level, "(event)", 0, f"{event} {brief}", (), None)
        record.extra_fields = line
        logger.handle(record)
    except Exception:  # noqa: BLE001 — logging must not break the action
        pass
