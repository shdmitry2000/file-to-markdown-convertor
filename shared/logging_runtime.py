"""Logging settings a global admin sets in the UI, applied live in every pod.

Five values, each with an env var that stays its seed (the chart's value applies
until an admin overrides it, and again after the override is cleared):

    level          LOG_LEVEL             DEBUG | INFO | WARNING | ERROR
    body_cut       AUDIT_LOG_BODY_CUT    cut request bodies and long values
    body_bytes     AUDIT_LOG_BODY_BYTES  the cut size, 0..65536
    read_events    LOG_READ_EVENTS       write the read events
    event_schema   LOG_EVENT_SCHEMA      native | cim

The overrides live in the control-plane settings table under one key
(``STORE_KEY``). The gateway and the evaluator read that table themselves
(``sync_from_store``); templates and markdown-api have no control-plane access,
so the gateway PUTs the overrides to their ``/logging`` route on save and again
whenever a pod reports a different fingerprint (a restarted pod starts from env).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
from typing import Any, Dict, Optional

from shared import log_events

logger = logging.getLogger(__name__)

STORE_KEY = "logging"
LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
SCHEMAS = ("native", "cim")
MAX_BODY_BYTES = 65536

#: key -> (env var, type, code default)
FIELDS: Dict[str, tuple] = {
    "level": ("LOG_LEVEL", str, "INFO"),
    "body_cut": ("AUDIT_LOG_BODY_CUT", bool, True),
    "body_bytes": ("AUDIT_LOG_BODY_BYTES", int, 1024),
    "read_events": ("LOG_READ_EVENTS", bool, True),
    "event_schema": ("LOG_EVENT_SCHEMA", str, "native"),
}

_overrides: Dict[str, Any] = {}


def validate(values: Dict[str, Any]) -> Dict[str, Any]:
    """Overrides in canonical form. Raises ValueError on an unknown key or bad value.
    A None value means "no override" and is dropped."""
    out: Dict[str, Any] = {}
    for key, value in (values or {}).items():
        if key not in FIELDS:
            raise ValueError(f"unknown logging setting: {key}")
        if value is None:
            continue
        kind = FIELDS[key][1]
        if kind is bool:
            if not isinstance(value, bool):
                raise ValueError(f"{key} must be true or false")
        elif kind is int:
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_BODY_BYTES:
                raise ValueError(f"{key} must be a whole number from 0 to {MAX_BODY_BYTES}")
        elif key == "level":
            value = str(value).upper()
            if value not in LEVELS:
                raise ValueError(f"level must be one of {', '.join(LEVELS)}")
        elif key == "event_schema":
            value = str(value).lower()
            if value not in SCHEMAS:
                raise ValueError(f"event_schema must be one of {', '.join(SCHEMAS)}")
        out[key] = value
    return out


def _env_value(key: str) -> Any:
    env, kind, default = FIELDS[key]
    raw = os.getenv(env)
    if raw is None or raw.strip() == "":
        return default
    if kind is bool:
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if kind is int:
        try:
            return int(raw)
        except ValueError:
            return default
    return raw.strip().upper() if key == "level" else raw.strip().lower()


def fingerprint(overrides: Dict[str, Any]) -> str:
    """Short, order-independent id of an override set: the gateway's sync compares it."""
    return hashlib.sha256(json.dumps(overrides, sort_keys=True).encode()).hexdigest()[:12]


def apply(overrides: Dict[str, Any]) -> Dict[str, Any]:
    """Make ``overrides`` (the whole set; {} = none) this process's logging settings."""
    global _overrides
    overrides = validate(overrides)
    _overrides = overrides
    env_names = {key: FIELDS[key][0] for key in FIELDS}
    log_events.set_overrides({env_names[k]: ("true" if v is True else "false" if v is False else str(v))
                              for k, v in overrides.items() if k != "level"})
    level = overrides.get("level") or _env_value("level")
    logging.getLogger().setLevel(getattr(logging, level, logging.INFO))
    return current()


def current() -> Dict[str, Any]:
    """Effective values, which of them are overridden, and the override fingerprint."""
    return {
        "values": {k: _overrides.get(k, _env_value(k)) for k in FIELDS},
        "overrides": dict(_overrides),
        "fingerprint": fingerprint(_overrides),
    }


def _internal_ok(header: Optional[str]) -> bool:
    expected = (os.getenv("INTERNAL_SERVICE_TOKEN") or "").strip()
    return not expected or hmac.compare_digest(expected, (header or "").strip())


def register_logging_route(app) -> None:
    """``GET``/``PUT /logging`` for a pod the gateway pushes to (templates, markdown-api).

    Same posture as ``/template-connector``: reachable inside the cluster only, and
    the PUT checks ``INTERNAL_SERVICE_TOKEN`` when the platform sets one."""
    from fastapi import Body, Header, HTTPException

    @app.get("/logging", tags=["admin"])
    async def logging_get():
        return current()

    @app.put("/logging", tags=["admin"])
    async def logging_put(body: Dict[str, Any] = Body(...),
                          x_internal_auth: Optional[str] = Header(None)):
        if not _internal_ok(x_internal_auth):
            raise HTTPException(status_code=403, detail="internal service authentication failed")
        before = current()
        try:
            after = apply(body.get("overrides") or {})
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if before["fingerprint"] != after["fingerprint"]:
            logger.warning("logging settings applied: %s", after["values"])
        return after


async def load_from_store() -> Dict[str, Any]:
    """The stored overrides ({} when none), straight from the control-plane table."""
    from shared.db.settings import make_settings_store

    stored = (await make_settings_store().get_all()).get(STORE_KEY) or {}
    if isinstance(stored, str):
        stored = json.loads(stored)
    return validate(stored)


async def sync_from_store(interval: float = 30.0) -> None:
    """Apply the stored overrides now and whenever they change (gateway, evaluator).
    Runs until cancelled; a read error keeps the current settings."""
    while True:
        try:
            stored = await load_from_store()
            if fingerprint(stored) != fingerprint(_overrides):
                apply(stored)
                logger.warning("logging settings applied: %s", current()["values"])
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — the env settings keep working
            logger.warning("could not read the logging settings: %s", exc)
        await asyncio.sleep(interval)
