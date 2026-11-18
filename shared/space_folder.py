"""The one rule for a space's folder on the project volume.

A space's files live in `<base>/<space_id>/`. Only space create makes that folder
(the gateway, `ProjectManager.create_project`). Every other writer, in the gateway,
the templates and the evaluator, makes folders only BELOW an existing space folder,
through `ensure_space_dir`.

Before this rule every writer did `mkdir(parents=True)`, so a late write for a
deleted space (a status read, an ingest heartbeat, an evaluation result) brought
its folder back with no owner. See docs/deployment/FILE_STORAGE.md.

On the S3 lane the templates may not share a volume with the gateway, so the
space folder proves nothing there and the guard steps aside.
"""

from __future__ import annotations

import os
from pathlib import Path


class SpaceGone(FileNotFoundError):
    """The space's folder does not exist: the space was deleted, or never made.

    A FileNotFoundError, so a writer that already treats an OSError as best effort
    logs it and goes on. Templates answer it with 404 (`install_space_lifecycle`).
    """

    def __init__(self, space: str, path: Path):
        super().__init__(f"space '{space}' has no folder (deleted?): {path}")
        self.space = str(space)
        self.path = path


def _guarded() -> bool:
    """Only where the space folder is the evidence: the shared volume (pvc)."""
    from shared.file_storage import ENV_BACKEND, resolve_backend

    try:
        return resolve_backend()[0] == "pvc"
    except Exception:  # noqa: BLE001 — a half S3 config fails boot elsewhere
        return (os.environ.get(ENV_BACKEND) or "pvc").strip().lower() == "pvc"


def _is_dir(p: Path) -> bool:
    return p.is_dir()


def _space_root(path: Path, space: str) -> Path:
    """The ancestor of `path` (or `path` itself) named after the space."""
    for p in (path, *path.parents):
        if p.name == space:
            return p
    raise ValueError(f"{path} is not inside a folder of space '{space}'")


def ensure_space_dir(path: Path | str, space: str) -> Path:
    """Make the folder `path` inside the space's folder, never the space folder.

    Raises SpaceGone when the space folder does not exist. The levels below it
    are made one at a time WITHOUT `parents=True`, so a space deleted midway
    (its rmtree running now) makes the next level fail instead of re-creating
    the space folder.
    """
    path = Path(path)
    space = str(space)
    if not _guarded():
        path.mkdir(parents=True, exist_ok=True)
        return path
    root = _space_root(path, space)
    if path == root:
        if not _is_dir(root):
            raise SpaceGone(space, root)
        return path
    if not _is_dir(root):
        raise SpaceGone(space, root)
    current = root
    for part in path.relative_to(root).parts:
        current = current / part
        try:
            current.mkdir(exist_ok=True)
        except FileNotFoundError as exc:
            raise SpaceGone(space, root) from exc
    return path
