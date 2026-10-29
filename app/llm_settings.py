"""The converter LLM: markdown-api's own LLM, used when a job brings none.

Its default is this pod's env, under the names spaces use — ``LLM_CONNECTOR_TYPE`` +
``LLM_TYPE`` (``VLM_MODEL`` still wins for the model), ``PROXY_BASE_URL``,
``PROXY_CONNECTOR_ID``, ``PROXY_AUTH_TOKEN``, ``GOOGLE_CLOUD_PROJECT_ID``,
``VERTEX_LOCATION`` — whatever type that is.

It can be replaced at runtime (``PUT /llm``) only when ``LLM_RUNTIME_CHANGE`` is on:
off in the product, where it comes from Helm and each space sends its own; on for a
standalone markdown-api with no product around it. The change lives in memory
(``LLM_SETTINGS_STORE=memory``, the default): a restart returns to the env. A file
store on the data volume is ready (``LLM_SETTINGS_STORE=file``) but not used yet; it
keeps the token encrypted with ``CONNECTOR_KEK`` and refuses to store one without it.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Optional

FIELDS = ("model", "base_url", "connector_id", "auth_token", "project", "location")


def runtime_change_enabled() -> bool:
    return os.getenv("LLM_RUNTIME_CHANGE", "false").strip().lower() in ("1", "true", "yes")


class MemoryStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._value: Optional[dict] = None

    def get(self) -> Optional[dict]:
        with self._lock:
            return dict(self._value) if self._value else None

    def set(self, value: dict) -> None:
        with self._lock:
            self._value = dict(value)

    def clear(self) -> None:
        with self._lock:
            self._value = None


class FileStore:
    """A JSON file on the data volume; the token sealed with CONNECTOR_KEK."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()

    def get(self) -> Optional[dict]:
        from shared import secret_box

        with self._lock:
            if not self._path.is_file():
                return None
            value = json.loads(self._path.read_text(encoding="utf-8"))
        if value.get("auth_token"):
            value["auth_token"] = secret_box.decrypt(value["auth_token"])
        return value

    def set(self, value: dict) -> None:
        from shared import secret_box

        stored = dict(value)
        if stored.get("auth_token"):
            if not secret_box.is_enabled():
                raise ValueError("CONNECTOR_KEK is not set: a token is never written to disk in clear")
            stored["auth_token"] = secret_box.encrypt(stored["auth_token"])
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(stored), encoding="utf-8")
            tmp.replace(self._path)

    def clear(self) -> None:
        with self._lock:
            self._path.unlink(missing_ok=True)


def _build_store():
    kind = os.getenv("LLM_SETTINGS_STORE", "memory").strip().lower()
    if kind == "file":
        base = os.getenv("CONVERTED_FILES_DIR") or "/app/projects/.cache"
        return FileStore(Path(os.getenv("LLM_SETTINGS_FILE") or Path(base) / "converter_llm.json"))
    return MemoryStore()


_store = _build_store()


def store():
    return _store


def runtime() -> Optional[dict]:
    """The LLM set at runtime, or None (the env default applies)."""
    return _store.get()


def env_default() -> dict:
    """The env default, as the fields a connector has."""
    from shared.connector_credentials import global_connector_creds

    from app.converters import vlm_lane

    g = global_connector_creds()
    return {"model": vlm_lane.pod_model(), "base_url": g.base_url,
            "connector_id": g.connector_id, "auth_token": g.auth_token,
            "project": g.project, "location": g.location}


def describe() -> dict:
    """What GET /llm shows: the converter LLM in use, never the token itself."""
    rt = runtime()
    try:
        current = rt or env_default()
        error = None
    except Exception as exc:  # noqa: BLE001 - an unconfigured pod still answers GET
        current, error = {}, str(exc).splitlines()[0]
    shown = {k: current.get(k) or None for k in FIELDS if k != "auth_token"}
    return {"source": "runtime" if rt else "env", **shown,
            "token_set": bool(current.get("auth_token")),
            "runtime_change": runtime_change_enabled(),
            "store": type(_store).__name__.replace("Store", "").lower(),
            **({"error": error} if error else {})}


def job_lane(sent: Optional[dict]) -> Optional[dict]:
    """The lane a job runs on: what the caller sent, else the runtime converter LLM
    (as params, so its identity is used), else None — the env default."""
    if sent:
        return sent
    rt = runtime()
    return {"mode": "params", **rt} if rt else None
