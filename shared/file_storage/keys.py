"""The storage layout, defined once.

Every project file lives at a **key** — a POSIX-style relative path such as
``<space>/raw/report.pdf``. On the PVC backend a key is joined onto
``PROJECTS_BASE_PATH``; on S3 it is joined onto the bucket prefix. Nothing outside
this module is allowed to know which.

The layout itself is unchanged from what ``shared.indexer_utils`` has always built,
which is what lets the PVC backend stay byte-for-byte compatible with the volume
already on disk.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Optional


class UnsafeKeyError(ValueError):
    """A key that escapes its prefix, or is absolute.

    Worth its own type because the one caller that takes a key from the network —
    markdown-api's ``/convert``, which today opens whatever absolute path it is
    handed — must reject rather than resolve.
    """


def normalize(key: str) -> str:
    """Return `key` as a safe relative POSIX key, or raise ``UnsafeKeyError``."""
    if not key or not key.strip():
        raise UnsafeKeyError("empty storage key")
    candidate = PurePosixPath(key.strip().replace("\\", "/"))
    if candidate.is_absolute():
        raise UnsafeKeyError(f"storage key must be relative, got {key!r}")
    if any(part == ".." for part in candidate.parts):
        raise UnsafeKeyError(f"storage key must not traverse upward, got {key!r}")
    # Drops "." segments and duplicate slashes; PurePosixPath does not resolve, which
    # is the point — nothing here may touch the filesystem to decide safety.
    return str(candidate)


def join(*parts: str) -> str:
    """Join key segments. A segment after the first may not be absolute.

    Stripping a leading slash instead would silently rebase it: an upload named
    "/etc/passwd" would become "<space>/raw/etc/passwd" — inside the space, so not
    an escape, but stored under a name nobody asked for and different from the one
    echoed back to the caller. Refusing keeps the name and the key the same thing.
    """
    present = [p for p in parts if p]
    for part in present[1:]:
        if part.startswith("/"):
            raise UnsafeKeyError(
                f"key segment must be relative, got {part!r}"
            )
    return normalize("/".join(p.strip("/") for p in present))


def raw_dir_key(space: str) -> str:
    """Original uploads for a space — PDFs and markdown as received."""
    return join(space, "raw")


def raw_key(space: str, filename: str) -> str:
    return join(raw_dir_key(space), filename)


def system_markdown_dir_key(space: str, system_id: Optional[str]) -> Optional[str]:
    """Converted markdown for one system. None when there is no system, matching
    ``get_system_markdown_dir``: each system keeps its own copy for isolation."""
    if not system_id:
        return None
    return join(space, system_id, "markdown")


def system_markdown_key(space: str, system_id: str, filename: str) -> str:
    return join(system_markdown_dir_key(space, system_id), filename)


def space_prefix(space: str) -> str:
    """Everything belonging to a space — the unit a space delete removes."""
    return normalize(space)


def system_prefix(space: str, system_id: str) -> str:
    return join(space, system_id)
