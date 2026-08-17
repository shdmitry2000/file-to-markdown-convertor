"""Cancelling a conversion, across the process boundary.

The API and the workers are separate processes (separate pods in Kubernetes), and
a worker mid-conversion is blocked inside `recv_string()`'s job — it is NOT
reading its ZeroMQ socket. So a cancel cannot travel on the task queue: PUSH/PULL
would hand it to an idle worker, or leave it queued behind the busy one until it
finishes, which is exactly when a cancel is worthless.

The signal therefore goes out of band, through the filesystem — which the API and
workers already share and already depend on. The API hands the worker a
`file_path` and reads back `{CONVERTED_FILES_DIR}/{stem}.md`; if that view were
not shared, conversion would not work at all. A marker file rides the same
assumption, needs no new port, thread or dependency, and can be inspected with
`ls` when something looks wrong.

If workers are ever split off the shared volume, `is_cancelled` and
`request_cancel` are the two functions to re-implement (a ZeroMQ PUB/SUB channel
being the obvious replacement); nothing else needs to know.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class ConversionCancelled(Exception):
    """Raised inside a worker when the job it is running has been cancelled.

    Distinct from a conversion FAILING: nothing is wrong with the document, so it
    is reported as `cancelled` rather than `failed` and never counted against the
    document's retry budget.
    """


def cancel_dir() -> Path:
    """Where cancel markers live. Overridable so a deployment can point it at a
    volume shared differently from the converted files."""
    configured = os.getenv("CANCEL_MARKER_DIR")
    if configured:
        return Path(configured)
    from app.config import get_settings

    return Path(get_settings().CONVERTED_FILES_DIR) / ".cancelled"


def _marker(conversion_id: str) -> Path:
    return cancel_dir() / conversion_id


def request_cancel(conversion_id: str) -> bool:
    """Mark a conversion cancelled. Returns whether the marker is in place.

    Written via a temp file and `os.replace` so a worker never observes a
    half-created marker — the reader only ever does an existence check, and a
    partially written name would be a different (never-checked) one anyway, but
    the atomic rename keeps that true by construction rather than by luck.
    """
    path = _marker(conversion_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        tmp.write_text("", encoding="utf-8")
        os.replace(tmp, path)
        return True
    except OSError as exc:
        # Never fail the request over this. The caller's own wait still ends —
        # they stop waiting on a conversion whose compute we could not reclaim,
        # which is the behaviour that predates this module.
        logger.warning("could not write cancel marker for %s: %s", conversion_id, exc)
        return False


def is_cancelled(conversion_id: str) -> bool:
    """Has this conversion been cancelled? One `stat`, safe to call in a poll loop."""
    try:
        return _marker(conversion_id).exists()
    except OSError:
        # An unreadable marker directory must not stop conversions from running.
        return False


def clear_cancel(conversion_id: str) -> None:
    """Drop the marker once the conversion has reached a terminal state.

    Best effort: a marker that outlives its job wastes an inode, while failing a
    completed conversion over a leftover file would waste the conversion.
    """
    try:
        _marker(conversion_id).unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.debug("could not clear cancel marker for %s: %s", conversion_id, exc)


def marker_ttl_seconds() -> int:
    """How long a marker can possibly still be needed.

    A conversion cannot outlive the worker's own hard timeout, so a marker older
    than that plus a margin has no job left to stop.
    """
    configured = os.getenv("CANCEL_MARKER_TTL_SECONDS")
    if configured:
        return int(configured)
    return int(os.getenv("DOCLING_TIMEOUT_SECONDS", "7200")) + 3600


def sweep(max_age_seconds: Optional[int] = None) -> int:
    """Delete markers too old to matter. Returns how many were removed.

    Terminal statuses clear their own marker, so this is for the ones that never
    get a terminal status: a cancel for a job that was never dispatched, or whose
    worker died, or — observed within minutes of this shipping — a service
    restarted between the cancel and the worker reporting back. Nothing else
    collects those, and they accumulate on a shared volume forever.
    """
    ttl = marker_ttl_seconds() if max_age_seconds is None else max_age_seconds
    cutoff = time.time() - ttl
    removed = 0
    try:
        entries = list(cancel_dir().iterdir())
    except (OSError, FileNotFoundError):
        return 0
    for entry in entries:
        try:
            if entry.is_file() and entry.stat().st_mtime < cutoff:
                entry.unlink()
                removed += 1
        except OSError:
            continue
    if removed:
        logger.info("swept %d stale cancel marker(s) older than %ds", removed, ttl)
    return removed
