"""Project-file storage: one contract, a backend chosen by environment.

    FILE_STORAGE_BACKEND=pvc|s3     default pvc — today's behaviour, unchanged

    # pvc
    PROJECTS_BASE_PATH=/app/projects        where the shared claim is mounted

    # s3 (ODF/NooBaa or MinIO on-prem, GCS's XML API on GKE)
    S3_ENDPOINT_URL=https://s3.internal.bank:443    required
    S3_BUCKET=ragp-projects                         required
    S3_PREFIX=rag-eval-bank                 optional; mirrors today's PVC subPath
    S3_REGION=us-east-1                     arbitrary for MinIO, sigv4 still needs one
    S3_ADDRESSING_STYLE=path                path unless the endpoint serves vhost buckets
    S3_VERIFY_SSL=/path/to/ca-bundle.pem    or true/false
    S3_ACCESS_KEY_ID / S3_SECRET_ACCESS_KEY         Secret; omit both to use the
                                            ambient credential chain (IRSA, workload
                                            identity), never one without the other
    FILE_STORAGE_SCRATCH_DIR=/tmp/ragp      emptyDir; where objects stage for the
                                            converters, which need a real file

Selection is fail-closed and global env-only, like the database DSN: a backend named
but not configured refuses to resolve and names the variable. There is deliberately
no fallback from s3 to the local disk — a half-configured object store quietly
writing to a pod's own filesystem looks healthy, serves reads back from cache, and
loses everything on the next restart.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

from shared.file_storage import keys
from shared.file_storage.base import FileStore, LocalFileStore, ObjectInfo
from shared.file_storage.s3 import S3FileStore

__all__ = [
    "FileStore",
    "LocalFileStore",
    "ObjectInfo",
    "S3FileStore",
    "StorageConfigError",
    "ENV_BACKEND",
    "ENV_PROJECTS_BASE_PATH",
    "ENV_PROJECTS_BASE_ANCHOR",
    "ENV_ALLOW_S3",
    "file_store",
    "preflight",
    "local_projects_root",
    "resolve_backend",
    "reset_file_store_cache",
    "keys",
]

ENV_BACKEND = "FILE_STORAGE_BACKEND"
ENV_PROJECTS_BASE_PATH = "PROJECTS_BASE_PATH"
ENV_PROJECTS_BASE_ANCHOR = "PROJECTS_BASE_ANCHOR"
ENV_ALLOW_S3 = "FILE_STORAGE_ALLOW_S3"
ENV_S3_ENDPOINT = "S3_ENDPOINT_URL"
ENV_S3_BUCKET = "S3_BUCKET"
ENV_S3_PREFIX = "S3_PREFIX"
ENV_S3_REGION = "S3_REGION"
ENV_S3_ADDRESSING_STYLE = "S3_ADDRESSING_STYLE"
ENV_S3_CHECKSUM_MODE = "S3_CHECKSUM_MODE"
ENV_S3_VERIFY_SSL = "S3_VERIFY_SSL"
ENV_S3_ACCESS_KEY = "S3_ACCESS_KEY_ID"
ENV_S3_SECRET_KEY = "S3_SECRET_ACCESS_KEY"
ENV_SCRATCH_DIR = "FILE_STORAGE_SCRATCH_DIR"

_BACKENDS = ("pvc", "s3")

logger = logging.getLogger(__name__)

_cached: Optional[FileStore] = None


class StorageConfigError(RuntimeError):
    """The configured storage backend cannot be built. Never degraded into a
    different backend — see the module docstring."""


def resolve_backend() -> tuple[str, str]:
    """Which backend to use, and why — the reason goes in the boot log.

    FILE_STORAGE_BACKEND is authoritative when set. When it is not, the backend is
    inferred from whether the object store is configured, so a lane that has an
    endpoint and a bucket does not also need a switch flipped to use them.

    Inference reads only S3_ENDPOINT_URL and S3_BUCKET together. Neither alone
    flips anything: a stray bucket name left in a ConfigMap must not silently move
    where a bank's documents live.
    """
    explicit = (os.environ.get(ENV_BACKEND) or "").strip().lower()
    if explicit:
        return explicit, f"{ENV_BACKEND}={explicit}"
    endpoint = (os.environ.get(ENV_S3_ENDPOINT) or "").strip()
    bucket = (os.environ.get(ENV_S3_BUCKET) or "").strip()
    if endpoint and bucket:
        return "s3", f"detected: {ENV_S3_ENDPOINT} and {ENV_S3_BUCKET} are both set"
    if endpoint or bucket:
        # Half-configured. Inferring s3 would fail at the next variable; inferring
        # pvc would ignore a deliberate intent. Say which half is missing.
        missing = ENV_S3_BUCKET if endpoint else ENV_S3_ENDPOINT
        raise StorageConfigError(
            f"object store is half-configured: {missing} is not set. Set it, or set "
            f"{ENV_BACKEND}=pvc to use the mounted claim."
        )
    return "pvc", f"default: no {ENV_S3_ENDPOINT}/{ENV_S3_BUCKET} configured"



def _relative_anchor() -> Path:
    """What a relative PROJECTS_BASE_PATH is relative TO.

    Never consulted for an absolute path, which is what every deployed lane
    uses — this exists for developer setups that configure a repo-relative root.
    """
    explicit = (os.environ.get(ENV_PROJECTS_BASE_ANCHOR) or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    # Deliberately NOT discovered. Locating a .env and taking its parent looked
    # like agreement but was not: a nested .env, a container without python-dotenv,
    # or an environment injected with no file at all each fall back to the cwd —
    # the very split this resolution exists to prevent, reintroduced silently and
    # only where those conditions happen to hold. Either the value is absolute, or
    # PROJECTS_BASE_ANCHOR says what it is relative to. Otherwise it is the cwd and
    # the caller is warned.
    return Path.cwd()


def local_projects_root(base: str) -> Path:
    """Resolve the local store's root to one absolute directory.

    An absolute base is used as given: /app/projects on every deployed lane, so
    this is a no-op there and the k8s behaviour is unchanged.

    A RELATIVE base used to be handed to LocalFileStore verbatim, which made the
    store root depend on each process's working directory. Services started from
    different directories then addressed the same key against different roots and
    silently got different stores — markdown-api, started from its own submodule
    directory, resolved `./data/projects` to a sibling that held no spaces at all,
    so every key lookup 404'd while the gateway wrote happily to the real one.
    Nothing errored: an empty store looks exactly like a store with nothing in it.

    Anchoring on the .env that supplied the value gives every process that reads
    that file the same root, whatever directory it was launched from.
    """
    path = Path(base).expanduser()
    if path.is_absolute():
        return path
    root = (_relative_anchor() / path).resolve()
    logger.warning(
        "%s=%r is relative; resolved to %s (anchor %s, cwd %s). Set it absolutely, "
        "or set %s, if processes may be started from different directories.",
        ENV_PROJECTS_BASE_PATH, base, root, _relative_anchor(), Path.cwd(),
        ENV_PROJECTS_BASE_ANCHOR,
    )
    return root


def _truthy(value: Optional[str]) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def _build() -> FileStore:
    backend, reason = resolve_backend()
    logger.info("file storage backend %r (%s)", backend, reason)
    if backend == "pvc":
        base = (os.environ.get(ENV_PROJECTS_BASE_PATH) or "").strip()
        if not base:
            raise StorageConfigError(
                f"{ENV_BACKEND}=pvc needs {ENV_PROJECTS_BASE_PATH} — the directory the "
                f"shared claim is mounted on (/app/projects in every deployed lane)."
            )
        root = local_projects_root(base)
        # The root itself, not just the backend name: two services can agree on
        # "pvc" and still be pointed at different directories, which is the one
        # failure this log makes obvious in a pod.
        logger.info("file storage root %s", root)
        return LocalFileStore(root)
    if backend == "s3":
        return _build_s3()
    raise StorageConfigError(
        f"unknown {ENV_BACKEND}={backend!r}; supported: {', '.join(_BACKENDS)}"
    )


def _required(name: str) -> str:
    value = (os.environ.get(name) or "").strip()
    if not value:
        raise StorageConfigError(f"{ENV_BACKEND}=s3 requires {name}")
    return value


def _build_s3() -> FileStore:
    from shared.file_storage.s3 import (
        S3FileStore, build_client, checksum_mode, verify_setting,
    )

    endpoint = _required(ENV_S3_ENDPOINT)
    bucket = _required(ENV_S3_BUCKET)
    access_key = (os.environ.get(ENV_S3_ACCESS_KEY) or "").strip()
    secret_key = (os.environ.get(ENV_S3_SECRET_KEY) or "").strip()
    if bool(access_key) != bool(secret_key):
        # Half a credential is never intentional, and boto3 would quietly fall back
        # to the ambient chain and fail later with a signature error instead.
        raise StorageConfigError(
            f"set both {ENV_S3_ACCESS_KEY} and {ENV_S3_SECRET_KEY}, or neither to use "
            f"the ambient credential chain"
        )

    # After the config checks above, never before them: a half-configured bucket
    # must still report THAT, rather than this. The s3 backend is not ingest-ready
    # — list, upload, delete and the converter address documents by key, but
    # rag_core's prepare, ingest, manifest and the gateway download still build
    # host paths from PROJECTS_BASE_PATH. On an object store the uploads land in
    # the bucket while ingest reads a local tree holding nothing: a lane that boots
    # clean and fails later, which is the failure this module exists to prevent.
    #
    # Nothing is removed to say so. S3FileStore, the ABC, key normalisation and
    # the s3 tests all stay, so finishing the migration means moving rag_core onto
    # keys and deleting this gate — not rebuilding the backend.
    if not _truthy(os.environ.get(ENV_ALLOW_S3)):
        raise StorageConfigError(
            f"{ENV_BACKEND}=s3 is not ingest-ready in this version: uploads would go "
            f"to the bucket while prepare/ingest/download still read "
            f"{ENV_PROJECTS_BASE_PATH}. Use the pvc backend, or set {ENV_ALLOW_S3}=true "
            f"if you are implementing the key migration."
        )
    logger.warning(
        "%s=true — s3 selected while rag_core still addresses documents by host "
        "path; ingest is expected to fail on this lane.", ENV_ALLOW_S3,
    )
    scratch = (os.environ.get(ENV_SCRATCH_DIR) or "").strip()
    return S3FileStore(
        bucket=bucket,
        prefix=(os.environ.get(ENV_S3_PREFIX) or "").strip(),
        scratch_dir=Path(scratch) if scratch else None,
        endpoint_for_display=endpoint,
        client=build_client(
            endpoint_url=endpoint,
            region=(os.environ.get(ENV_S3_REGION) or "us-east-1").strip(),
            # Path-style by default: MinIO and NooBaa generally do not serve
            # virtual-host buckets, and that mismatch reads as a 404 on every call.
            addressing_style=(os.environ.get(ENV_S3_ADDRESSING_STYLE) or "path").strip(),
            verify=verify_setting(os.environ.get(ENV_S3_VERIFY_SSL)),
            access_key=access_key,
            secret_key=secret_key,
            checksum_mode=checksum_mode(os.environ.get(ENV_S3_CHECKSUM_MODE)),
        ),
    )


def file_store() -> FileStore:
    """The process-wide store. Built once; the configuration is global env-only."""
    global _cached
    if _cached is None:
        _cached = _build()
    return _cached


def reset_file_store_cache() -> None:
    """Drop the cached store (tests, and anything that rewrites the environment)."""
    global _cached
    _cached = None


def preflight(store: Optional[FileStore] = None) -> str:
    """Prove the configured backend is writable, and return a line for the boot log.

    The chart already runs an initContainer that touches the mount root for the PVC
    lane. This is that check for whichever backend is configured, in-process and
    after credentials are resolved — so a wrong bucket, an untrusted CA, or
    virtual-host addressing against a path-style endpoint fails at boot naming the
    cause, rather than on the first user upload hours later.

    The probe key lives under a reserved prefix and is removed again; a backend that
    cannot complete the round trip raises.
    """
    if store is None:
        backend, reason = resolve_backend()
        if backend == "pvc" and not (os.environ.get(ENV_PROJECTS_BASE_PATH) or "").strip():
            # No volume was configured, so there is nothing to prove writable. This
            # mirrors require_writable_project_data(): an unset PROJECTS_BASE_PATH
            # means a local run writing under the repo, not a deployment — and a
            # boot check must not be the thing that stops `uv run` from starting.
            return f"storage unconfigured — {reason}; nothing to probe"
        store = file_store()
    target = store
    probe = f".preflight/{os.getpid()}"
    try:
        target.write_bytes(probe, b"ok")
        if target.read_bytes(probe) != b"ok":
            raise StorageConfigError(
                f"storage preflight read back the wrong bytes ({target.describe()})"
            )
    except StorageConfigError:
        raise
    except Exception as exc:  # noqa: BLE001 — every backend failure means the same thing here
        raise StorageConfigError(
            f"storage preflight failed for {target.describe()}: {exc}"
        ) from exc
    finally:
        try:
            target.delete(probe)
        except Exception:  # noqa: BLE001 — a stranded probe key must not fail boot
            logger.warning("could not remove storage preflight key %s", probe)
    return f"storage ready — {target.describe()}"
