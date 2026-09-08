"""Optional access to the platform's FileStore.

This image is built two ways. From the enterprise repo the sync vendors
`shared/file_storage/`; a standalone submodule build has no `shared/` at all. So the
import is guarded exactly like the telemetry one: absent means the object-store path
is unavailable, not that the service is broken.

Callers that pass `file_path` never come through here — the shared-claim lane works
in either build, unchanged.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

_UNSET = object()
_cached: Any = _UNSET


class StorageUnavailable(RuntimeError):
    """A request supplied a storage key and this build cannot resolve one."""


def file_store() -> Optional[Any]:
    """The configured FileStore, or None when this build has no shared/file_storage.

    A configuration error (an object store named but not configured) is NOT None —
    it propagates, because falling back to "unavailable" would turn a deployment
    mistake into a 503 that reads like a missing feature.
    """
    global _cached
    if _cached is _UNSET:
        try:
            from shared.file_storage import file_store as _factory
        except ImportError:
            logger.info(
                "shared.file_storage not vendored in this build — storage keys "
                "unavailable, file_path still works"
            )
            _cached = None
            return None
        _cached = _factory
    if _cached is None:
        return None
    return _cached()


def reset() -> None:
    """Drop the cached factory (tests)."""
    global _cached
    _cached = _UNSET


def normalize_key(key: str) -> str:
    """Validate a key from the network. Raises StorageUnavailable when this build
    cannot resolve keys at all, so the caller can answer 503 rather than 400."""
    try:
        from shared.file_storage import keys
    except ImportError as exc:
        raise StorageUnavailable(
            "this markdown-api build cannot resolve storage keys; send file_path, "
            "or deploy the image built with shared/file_storage vendored"
        ) from exc
    return keys.normalize(key)
