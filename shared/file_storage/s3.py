"""S3-compatible backend: ODF/NooBaa or MinIO on-prem, GCS's XML API on GKE.

Three defaults here are chosen for on-prem rather than for AWS, because on-prem is
where this has to work:

* **path-style addressing.** boto3 prefers virtual-host style (``bucket.host``),
  which MinIO and NooBaa generally do not serve. That mismatch presents as 404s on
  every operation against an endpoint that is plainly reachable, so ``path`` is the
  default and ``S3_ADDRESSING_STYLE`` exists to opt back out.
* **Content-MD5 checksums.** boto3 1.36 switched to ``x-amz-checksum-crc32``; the
  Hitachi S3 array in production predates that, and MinIO rejects it outright on
  DeleteObjects. ``S3_CHECKSUM_MODE`` defaults to the header every implementation
  accepts, and opts back into CRC32 for an array that handles it.
* **TLS verification pointed at a CA bundle.** An internal endpoint uses an internal
  CA. ``S3_VERIFY_SSL`` takes a path to the bundle; it also accepts false, which
  should be a last resort and never a fix in this environment.

`materialize` stages the object into `FILE_STORAGE_SCRATCH_DIR` because docling and
PyMuPDF need a real file. Scratch is per-pod and disposable — an emptyDir, never the
durable copy.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO, Iterator, Optional

from shared.file_storage import keys
from shared.file_storage.base import CHUNK_SIZE, FileStore, ObjectInfo

logger = logging.getLogger(__name__)

#: S3 deletes at most this many keys per DeleteObjects call.
_DELETE_BATCH = 1000


class S3FileStore(FileStore):
    def __init__(
        self,
        *,
        bucket: str,
        client: Any,
        prefix: str = "",
        scratch_dir: Optional[Path] = None,
        endpoint_for_display: str = "",
    ) -> None:
        self._bucket = bucket
        self._client = client
        self._prefix = prefix.strip("/")
        self._scratch = Path(scratch_dir) if scratch_dir else None
        self._endpoint = endpoint_for_display

    # -- key <-> object name -------------------------------------------------------

    def _object(self, key: str) -> str:
        safe = keys.normalize(key)
        return f"{self._prefix}/{safe}" if self._prefix else safe

    def _key(self, object_name: str) -> str:
        if self._prefix and object_name.startswith(f"{self._prefix}/"):
            return object_name[len(self._prefix) + 1:]
        return object_name

    def _object_prefix(self, prefix: str) -> str:
        # Trailing slash so "space1" cannot also match "space10".
        return self._object(prefix).rstrip("/") + "/"

    # -- reads ---------------------------------------------------------------------

    def read_bytes(self, key: str) -> bytes:
        try:
            return self._client.get_object(Bucket=self._bucket, Key=self._object(key))["Body"].read()
        except Exception as exc:  # noqa: BLE001 — normalized below
            if _is_not_found(exc):
                raise FileNotFoundError(f"no object at key {key!r}") from exc
            raise

    def iter_chunks(self, key: str, chunk_size: int = CHUNK_SIZE) -> Iterator[bytes]:
        try:
            body = self._client.get_object(Bucket=self._bucket, Key=self._object(key))["Body"]
        except Exception as exc:  # noqa: BLE001 — normalized below
            if _is_not_found(exc):
                raise FileNotFoundError(f"no object at key {key!r}") from exc
            raise
        try:
            yield from body.iter_chunks(chunk_size)
        finally:
            body.close()

    def stat(self, key: str) -> Optional[ObjectInfo]:
        try:
            head = self._client.head_object(Bucket=self._bucket, Key=self._object(key))
        except Exception as exc:  # noqa: BLE001
            if _is_not_found(exc):
                return None
            raise
        modified = head.get("LastModified")
        return ObjectInfo(
            key=keys.normalize(key),
            size=int(head.get("ContentLength", 0)),
            modified=modified.timestamp() if hasattr(modified, "timestamp") else 0.0,
        )

    def list_entries(self, prefix: str) -> list[ObjectInfo]:
        entries: list[ObjectInfo] = []
        for page in self._pages(self._object_prefix(prefix)):
            for item in page.get("Contents", []):
                modified = item.get("LastModified")
                entries.append(ObjectInfo(
                    key=self._key(item["Key"]),
                    size=int(item.get("Size", 0)),
                    # ListObjectsV2 already carries both, so no HeadObject per key.
                    modified=modified.timestamp() if hasattr(modified, "timestamp") else 0.0,
                ))
        return sorted(entries, key=lambda e: e.key)

    def _pages(self, object_prefix: str) -> Iterator[dict]:
        paginator = self._client.get_paginator("list_objects_v2")
        yield from paginator.paginate(Bucket=self._bucket, Prefix=object_prefix)

    # -- writes --------------------------------------------------------------------

    def write_bytes(self, key: str, data: bytes) -> None:
        # A PUT is atomic at the object level, so unlike the local backend there is no
        # temp-then-rename dance: a reader sees the old object or the new one.
        self._client.put_object(Bucket=self._bucket, Key=self._object(key), Body=data)

    def write_fileobj(self, key: str, fileobj: BinaryIO) -> None:
        # boto3 reads a seekable file body in chunks; it is never held whole.
        self._client.put_object(Bucket=self._bucket, Key=self._object(key), Body=fileobj)

    def delete(self, key: str) -> None:
        # Absent is success — S3 DeleteObject is already idempotent.
        self._client.delete_object(Bucket=self._bucket, Key=self._object(key))

    def delete_prefix(self, prefix: str) -> int:
        removed = 0
        errors: list[dict] = []
        batch: list[dict] = []
        for page in self._pages(self._object_prefix(prefix)):
            for item in page.get("Contents", []):
                batch.append({"Key": item["Key"]})
                if len(batch) == _DELETE_BATCH:
                    removed += self._delete_batch(batch, errors)
                    batch = []
        removed += self._delete_batch(batch, errors)
        if errors:
            # A partial delete must not read as a clean one (space delete reports
            # this count): every batch was still tried, then the failure is raised.
            first = errors[0]
            raise RuntimeError(
                f"{len(errors)} object(s) under {prefix!r} not deleted ({removed} were); "
                f"first: {first.get('Key')}: {first.get('Code')} {first.get('Message', '')}".rstrip())
        return removed

    def _delete_batch(self, batch: list[dict], errors: list[dict]) -> int:
        if not batch:
            return 0
        # DeleteObjects does not raise for a key it could not delete: it lists it
        # under Errors, so only what is not there counts as removed.
        response = self._client.delete_objects(Bucket=self._bucket, Delete={"Objects": batch})
        failed = (response or {}).get("Errors") or []
        errors.extend(failed)
        return len(batch) - len(failed)

    def copy_prefix(self, source: str, destination: str) -> int:
        src_prefix = self._object_prefix(source)
        dst_prefix = self._object_prefix(destination)
        copied = 0
        for page in self._pages(src_prefix):
            for item in page.get("Contents", []):
                relative = item["Key"][len(src_prefix):]
                # Server-side: the bytes never travel through this process, which is
                # what makes cloning a large space affordable. The managed copy, not
                # one CopyObject: that caps at 5 GB, this goes multipart above it.
                self._client.copy(
                    CopySource={"Bucket": self._bucket, "Key": item["Key"]},
                    Bucket=self._bucket,
                    Key=f"{dst_prefix}{relative}",
                )
                copied += 1
        return copied

    # -- local materialization -----------------------------------------------------

    @contextmanager
    def materialize(self, key: str) -> Iterator[Path]:
        data = self.read_bytes(key)  # raises FileNotFoundError when absent
        if self._scratch is not None:
            self._scratch.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix="ragp-", dir=self._scratch))
        try:
            # Keep the original name: converters branch on the suffix, and error
            # messages that name "report.pdf" beat ones naming "tmp7f3a".
            local = staging / Path(keys.normalize(key)).name
            local.write_bytes(data)
            yield local
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def local_path(self, key: str) -> Optional[Path]:
        return None  # no filesystem: callers must use materialize()

    def describe(self) -> str:
        where = f"{self._endpoint}/{self._bucket}" if self._endpoint else self._bucket
        return f"s3: {where}" + (f"/{self._prefix}" if self._prefix else "")


def _is_not_found(exc: BaseException) -> bool:
    """404/NoSuchKey across botocore's several spellings, without importing it."""
    code = getattr(exc, "response", {}).get("Error", {}).get("Code") if hasattr(exc, "response") else None
    if code in {"404", "NoSuchKey", "NotFound"}:
        return True
    return type(exc).__name__ in {"NoSuchKey", "ClientError404"}


def verify_setting(raw: Optional[str]) -> Any:
    """Translate S3_VERIFY_SSL into what botocore's `verify` expects."""
    value = (raw or "").strip()
    if not value:
        return True
    if value.lower() in {"1", "true", "yes"}:
        return True
    if value.lower() in {"0", "false", "no"}:
        logger.warning(
            "S3 TLS verification is disabled (S3_VERIFY_SSL=%s). Point this at the "
            "internal CA bundle instead; disabling it is not a fix.", value,
        )
        return False
    return value  # a path to a CA bundle


#: How the DeleteObjects (and optionally every) request is checksummed.
#:
#: legacy      Content-MD5 on every request that carries a body. THE DEFAULT, because
#:             the production array is a Hitachi S3 and an unconfigured deployment
#:             must reach it: MD5 is what S3 required before boto3 1.36, so every
#:             implementation accepts it. Costs a little CPU on upload versus CRC32.
#: delete-md5  Content-MD5 on DeleteObjects only, the SDK's checksum elsewhere. For an
#:             array that handles CRC32 uploads — keeps the stronger, cheaper checksum
#:             where the bytes actually are.
#: sdk         Whatever boto3 does unaided. Correct against AWS, and the way back to
#:             stock behaviour if a future array prefers CRC32.
CHECKSUM_MODES = ("delete-md5", "legacy", "sdk")


def checksum_mode(raw: Optional[str]) -> str:
    """Validate S3_CHECKSUM_MODE, defaulting to the on-prem-safe one.

    Rejected at boot rather than ignored: a typo here would otherwise surface much
    later as MissingContentMD5 on a delete, which reads as a broken array rather
    than a misspelt setting.
    """
    value = (raw or "").strip().lower()
    if not value:
        return "legacy"
    if value not in CHECKSUM_MODES:
        from shared.file_storage import StorageConfigError

        raise StorageConfigError(
            f"S3_CHECKSUM_MODE={value!r} is not one of {', '.join(CHECKSUM_MODES)}"
        )
    return value


def build_client(
    *,
    endpoint_url: str,
    region: str,
    addressing_style: str,
    verify: Any,
    access_key: Optional[str],
    secret_key: Optional[str],
    checksum_mode: str = "legacy",
) -> Any:
    import boto3
    from botocore.config import Config

    client = boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        region_name=region,
        verify=verify,
        aws_access_key_id=access_key or None,
        aws_secret_access_key=secret_key or None,
        config=Config(
            s3={"addressing_style": addressing_style},
            retries={"max_attempts": 5, "mode": "standard"},
        ),
    )
    if checksum_mode == "delete-md5":
        client.meta.events.register("before-call.s3.DeleteObjects", force_content_md5)
    elif checksum_mode == "legacy":
        # Every operation that carries a body — uploads included.
        client.meta.events.register("before-call.s3.*", force_content_md5)
    return client


def force_content_md5(params: dict, **_: Any) -> None:
    """Replace boto3's CRC32 checksum header with the legacy `Content-MD5`.

    botocore 1.36 changed the default to `x-amz-checksum-crc32`. AWS takes either,
    but older S3-compatible arrays require the legacy header and reject the call
    outright with `MissingContentMD5` — verified against MinIO, where it breaks
    `delete_prefix`, and so deleting a space.

    `request_checksum_calculation="when_required"` does NOT help: DeleteObjects is
    an operation that *requires* a checksum, so botocore still sends CRC32. The
    header has to be replaced on the way out.

    Integrity is unchanged — MD5 over the same body, which is what the API asked
    for before 1.36. `usedforsecurity=False` keeps it legal on a FIPS host, where
    this is a checksum and not a signature.

    Only a bytes/str body is hashed. A streaming upload arrives as a file object,
    and reading it here would consume it before it is sent; those keep the SDK's
    own checksum rather than being silently corrupted.
    """
    body = params.get("body")
    if not isinstance(body, (bytes, bytearray, str)):
        return  # absent, or a stream this must not consume
    if isinstance(body, str):
        body = body.encode()
    headers = params.setdefault("headers", {})
    for name in [h for h in headers
                 if h.lower().startswith("x-amz-checksum-")
                 or h.lower() == "x-amz-sdk-checksum-algorithm"]:
        headers.pop(name)
    headers["Content-MD5"] = base64.b64encode(
        hashlib.md5(bytes(body), usedforsecurity=False).digest()
    ).decode()
