"""The FileStore contract, and the local-filesystem implementation of it.

`FileStore` is deliberately small: it carries exactly the operations the three edges
need — the gateway (upload, list, delete, clone), markdown-api (read a PDF, write a
`.md`) and the templates (read a `.md` before chunking). Everything inside those
components keeps working on ordinary local paths.

`materialize` is the seam that makes that possible. docling and PyMuPDF need a real
file on disk, so an object store has to land bytes somewhere local before a converter
can touch them. On the PVC backend the file is already local and `materialize` yields
the mounted path directly — no copy, no scratch space, and the PVC lane costs exactly
what it costs today.
"""

from __future__ import annotations

import shutil
from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterator, Optional

from shared.file_storage import keys


@dataclass(frozen=True)
class ObjectInfo:
    """One stored object. `modified` is a POSIX timestamp.

    Size and mtime are here because the gateway's file listing returns them, and
    both backends can supply them without an extra round trip — S3's
    ListObjectsV2 already carries Size and LastModified per key.
    """

    key: str
    size: int
    modified: float


#: Chunk size for streamed reads and writes.
CHUNK_SIZE = 1024 * 1024


class FileStore(ABC):
    """Durable storage for project files, addressed by key (see `keys`)."""

    @abstractmethod
    def read_bytes(self, key: str) -> bytes: ...

    @abstractmethod
    def write_bytes(self, key: str, data: bytes) -> None: ...

    def write_fileobj(self, key: str, fileobj: BinaryIO) -> None:
        """Write from a readable binary file, without holding it whole in memory where
        the backend can stream (an upload can be hundreds of MB). Default: read it."""
        self.write_bytes(key, fileobj.read())

    def iter_chunks(self, key: str, chunk_size: int = CHUNK_SIZE) -> Iterator[bytes]:
        """The object's bytes in chunks, for a download that must not hold the whole
        file in memory. Default: one chunk."""
        yield self.read_bytes(key)

    @abstractmethod
    def stat(self, key: str) -> Optional[ObjectInfo]:
        """Metadata for one object, or None when it is absent.

        Separate from `list_entries` because the converter's cache check asks about
        exactly one key and comparing an mtime should not cost a directory walk.
        """

    def exists(self, key: str) -> bool:
        return self.stat(key) is not None

    @abstractmethod
    def list_entries(self, prefix: str) -> list[ObjectInfo]:
        """Every object under `prefix`, recursively. Directories are not objects."""

    def list_keys(self, prefix: str) -> list[str]:
        """Just the keys — `list_entries` when size and mtime are not needed."""
        return [entry.key for entry in self.list_entries(prefix)]

    @abstractmethod
    def delete(self, key: str) -> None:
        """Remove one key. Absent is success — deleting twice must not raise."""

    @abstractmethod
    def delete_prefix(self, prefix: str) -> int:
        """Remove everything under `prefix`; returns how many keys went."""

    @abstractmethod
    def copy_prefix(self, source: str, destination: str) -> int:
        """Copy every key under `source` to the same relative place under
        `destination`; returns how many were copied. This is a space/system clone."""

    @contextmanager
    @abstractmethod
    def materialize(self, key: str) -> Iterator[Path]:
        """Yield a readable local path for `key`.

        On the PVC backend this is the stored file itself, so callers must treat it
        as read-only. Backends that stage a copy clean it up on exit.
        """

    @abstractmethod
    def local_path(self, key: str) -> Optional[Path]:
        """The path this key occupies on a mounted filesystem, or None when the
        backend has no filesystem. Only for code that cannot yet take a key; every
        new caller should use `materialize`."""

    @abstractmethod
    def describe(self) -> str:
        """One line naming the backend and where it points, for boot logs."""


class LocalFileStore(FileStore):
    """Keys resolved against a mounted directory — the PVC (and local dev) backend."""

    def __init__(self, base: Path) -> None:
        self._base = Path(base)

    def _path(self, key: str) -> Path:
        return self._base / keys.normalize(key)

    def read_bytes(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def write_bytes(self, key: str, data: bytes) -> None:
        path = self._writable_path(key)
        # Same-directory temp + atomic replace: a reader polling for the converted
        # markdown must never observe a half-written file.
        tmp = path.with_name(f".{path.name}.partial")
        tmp.write_bytes(data)
        tmp.replace(path)

    def write_fileobj(self, key: str, fileobj: BinaryIO) -> None:
        path = self._writable_path(key)
        tmp = path.with_name(f".{path.name}.partial")
        with open(tmp, "wb") as out:
            shutil.copyfileobj(fileobj, out, CHUNK_SIZE)
        tmp.replace(path)

    def iter_chunks(self, key: str, chunk_size: int = CHUNK_SIZE) -> Iterator[bytes]:
        with open(self._path(key), "rb") as f:
            while chunk := f.read(chunk_size):
                yield chunk

    def _writable_path(self, key: str) -> Path:
        """The key's path, its folder made — only inside an existing space folder."""
        path = self._path(key)
        first = keys.normalize(key).split("/", 1)[0]
        if first.startswith("."):  # service files in the root (.preflight/…), not a space
            path.parent.mkdir(parents=True, exist_ok=True)
        else:  # `<space>/…`: only inside an existing space folder
            from shared.space_folder import ensure_space_dir

            ensure_space_dir(path.parent, first)
        return path

    def stat(self, key: str) -> Optional[ObjectInfo]:
        path = self._path(key)
        if not path.is_file():
            return None
        st = path.stat()
        return ObjectInfo(key=keys.normalize(key), size=st.st_size, modified=st.st_mtime)

    def list_entries(self, prefix: str) -> list[ObjectInfo]:
        root = self._path(prefix)
        if not root.is_dir():
            return []
        entries = []
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            stat = path.stat()
            entries.append(ObjectInfo(
                key=keys.join(keys.normalize(prefix), str(path.relative_to(root))),
                size=stat.st_size,
                modified=stat.st_mtime,
            ))
        return sorted(entries, key=lambda e: e.key)

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def delete_prefix(self, prefix: str) -> int:
        root = self._path(prefix)
        if not root.is_dir():
            return 0
        count = sum(1 for p in root.rglob("*") if p.is_file())
        shutil.rmtree(root, ignore_errors=True)
        # rmtree ignored its errors: count only what is actually gone.
        left = sum(1 for p in root.rglob("*") if p.is_file()) if root.exists() else 0
        return count - left

    def copy_prefix(self, source: str, destination: str) -> int:
        src, dst = self._path(source), self._path(destination)
        if not src.is_dir():
            return 0
        shutil.copytree(src, dst, dirs_exist_ok=True)
        return sum(1 for p in dst.rglob("*") if p.is_file())

    @contextmanager
    def materialize(self, key: str) -> Iterator[Path]:
        path = self._path(key)
        if not path.is_file():
            raise FileNotFoundError(f"no object at key {key!r} ({path})")
        yield path  # already local: no copy, no scratch directory, no cleanup

    def local_path(self, key: str) -> Optional[Path]:
        return self._path(key)

    def describe(self) -> str:
        return f"pvc: {self._base}"
