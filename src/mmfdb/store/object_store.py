"""Content-addressed local and S3-compatible blob storage.

This module provides local and remote backends for storing binary blobs
addressed by an MD5 routing digest and verified with SHA-256. Blob bytes are
deduplicated here; logical reference counts remain a repository concern.

Storage layout::

    {root}/{md5[:2]}/{md5[2:4]}/{md5}

Each stored object is mapped to a UUID in the ``mmfdb_object`` database table.
All cross-references use the UUID, never the MD5 directly.
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import re
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Protocol, runtime_checkable
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

_MD5_RE = re.compile(r"[0-9a-f]{32}\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


def _validate_md5(md5: str) -> str:
    """Return a canonical digest or reject path-like/untrusted input."""
    if not isinstance(md5, str) or _MD5_RE.fullmatch(md5) is None:
        raise ValueError("MD5 content address must be exactly 32 lowercase hex characters")
    return md5


def _validate_sha256(sha256: str) -> str:
    """Validate a strong content digest supplied by trusted database metadata."""
    if not isinstance(sha256, str) or _SHA256_RE.fullmatch(sha256) is None:
        raise ValueError("SHA-256 digest must be exactly 64 lowercase hex characters")
    return sha256


def _relative_address(md5: str) -> str:
    digest = _validate_md5(md5)
    return f"{digest[:2]}/{digest[2:4]}/{digest}"


@dataclass
class ObjectRef:
    """Reference to a stored object.

    Parameters
    ----------
    uuid : str
        Stable UUID for cross-references.
    md5 : str
        MD5 hex digest of the content (32 characters).
    sha256 : str
        Strong SHA-256 content digest (64 characters).
    size : int
        Size in bytes.
    original_filename : str or None
        Original filename at time of upload.
    deduplicated : bool
        True if the blob already existed (refcount incremented).
    storage_path : str
        Backend location for diagnostics; callers must retrieve through the
        backend rather than interpreting this value.
    """

    uuid: str
    md5: str
    sha256: str
    size: int
    original_filename: str | None
    deduplicated: bool
    storage_path: str


@runtime_checkable
class ObjectStoreBackend(Protocol):
    """Backend contract consumed by :class:`~mmfdb.repository.MFDatabase`.

    ``get_path`` always returns a readable local path. Remote implementations
    satisfy that contract through a verified cache. ``materialize`` writes a
    verified copy to a caller-selected path, which is preferred when a suffix
    or lifecycle boundary matters.
    """

    def put_from_path(self, path: Path, original_filename: str | None = None) -> ObjectRef:
        """Store a local file."""
        ...

    def put_bytes(self, data: bytes, filename: str) -> ObjectRef:
        """Store an immutable byte payload."""
        ...

    def get(self, md5: str, *, sha256: str | None = None) -> bytes:
        """Return a verified payload."""
        ...

    def get_path(self, md5: str, *, sha256: str | None = None) -> Path:
        """Return a verified readable local path."""
        ...

    def materialize(
        self,
        md5: str,
        destination: Path,
        *,
        sha256: str | None = None,
    ) -> Path:
        """Write a verified payload to a caller-owned local path."""
        ...

    def exists(self, md5: str) -> bool:
        """Report whether a content address exists."""
        ...

    def delete(self, md5: str) -> bool:
        """Delete a content address if present."""
        ...

    def verify(self, md5: str, sha256: str) -> bool:
        """Verify the address and strong digest."""
        ...


class ObjectStore:
    """Content-addressed object storage with MD5 deduplication.

    Parameters
    ----------
    root : Path
        Root directory for object storage.
    chunk_size : int
        Size of chunks for streaming reads (default 64KB).

    Examples
    --------
    >>> from pathlib import Path
    >>> import tempfile
    >>> with tempfile.TemporaryDirectory() as tmp:
    ...     store = ObjectStore(Path(tmp))
    ...     ref = store.put_bytes(b"hello world", filename="test.txt")
    ...     ref.md5
    '5eb63bbbe01eeed093cb22bb8f5acdc3'
    """

    def __init__(self, root: Path, chunk_size: int = 65536):
        self.root = Path(root)
        self.chunk_size = max(1, int(chunk_size))
        self.root.mkdir(parents=True, exist_ok=True)

    def _blob_path(self, md5: str) -> Path:
        """Return the filesystem path for a given MD5 hash."""
        digest = self._validate_md5(md5)
        path = self.root / digest[:2] / digest[2:4] / digest
        try:
            path.resolve().relative_to(self.root.resolve())
        except ValueError as exc:  # defensive containment check
            raise ValueError("MD5 content address escapes the object-store root") from exc
        return path

    def _relative_path(self, md5: str) -> str:
        """Return the relative storage path for a given MD5 hash."""
        return _relative_address(md5)

    @staticmethod
    def _validate_md5(md5: str) -> str:
        """Return a canonical digest or reject path-like/untrusted input."""
        return _validate_md5(md5)

    def _publish(self, blob_path: Path, writer) -> None:
        """Write and fsync a same-directory temp file, then publish atomically."""
        blob_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=blob_path.parent,
            prefix=f".{blob_path.name}.",
            suffix=".tmp",
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as target:
                writer(target)
                target.flush()
                os.fsync(target.fileno())
            os.replace(tmp_path, blob_path)
            try:
                dir_fd = os.open(blob_path.parent, os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError:
                # Directory fsync is unavailable on some supported platforms.
                pass
        finally:
            if tmp_path.exists():
                tmp_path.unlink()

    def _compute_md5_streaming(self, path: Path) -> str:
        """Compute MD5 hash of a file using streaming reads."""
        hasher = hashlib.md5(usedforsecurity=False)
        with open(path, "rb") as f:
            while chunk := f.read(self.chunk_size):
                hasher.update(chunk)
        return hasher.hexdigest()

    def put_from_path(
        self,
        path: Path,
        original_filename: str | None = None,
    ) -> ObjectRef:
        """Store a file in the object store.

        Reads the file in chunks, computes the MD5 hash, and stores the blob
        at the content-addressed path. If the same content already exists,
        the reference count is incremented instead of creating a duplicate.

        Parameters
        ----------
        path : Path
            Path to the file to store.
        original_filename : str, optional
            Original filename to record in metadata. Defaults to ``path.name``.

        Returns
        -------
        ObjectRef
            Reference to the stored object.

        Raises
        ------
        FileNotFoundError
            If the source file does not exist.
        """
        path = Path(path)
        if not path.is_file():
            raise FileNotFoundError(f"Source file not found: {path}")

        incoming = self.root / ".incoming"
        incoming.mkdir(parents=True, exist_ok=True)
        fd, staged_name = tempfile.mkstemp(dir=incoming, prefix="blob-", suffix=".tmp")
        staged = Path(staged_name)
        md5_hasher = hashlib.md5(usedforsecurity=False)
        sha256_hasher = hashlib.sha256()
        size = 0
        try:
            with os.fdopen(fd, "wb") as target:
                with path.open("rb") as source:
                    while chunk := source.read(self.chunk_size):
                        md5_hasher.update(chunk)
                        sha256_hasher.update(chunk)
                        target.write(chunk)
                        size += len(chunk)
                target.flush()
                os.fsync(target.fileno())
            md5 = md5_hasher.hexdigest()
            sha256 = sha256_hasher.hexdigest()
            blob_path = self._blob_path(md5)
            deduplicated = blob_path.exists()
            if deduplicated:
                if self._compute_sha256_streaming(blob_path) != sha256:
                    raise RuntimeError("MD5 collision detected; refusing unsafe deduplication")
                staged.unlink()
                logger.info("Deduplicated object: %s (refcount++)", md5)
            else:
                blob_path.parent.mkdir(parents=True, exist_ok=True)
                os.replace(staged, blob_path)
                logger.info("Stored new object: %s (%d bytes)", md5, size)
        finally:
            if staged.exists():
                staged.unlink()

        object_uuid = str(uuid.uuid4())
        original_filename = original_filename or path.name
        storage_path = self._relative_path(md5)

        return ObjectRef(
            uuid=object_uuid,
            md5=md5,
            sha256=sha256,
            size=size,
            original_filename=original_filename,
            deduplicated=deduplicated,
            storage_path=storage_path,
        )

    def put_bytes(
        self,
        data: bytes,
        filename: str,
    ) -> ObjectRef:
        """Store bytes directly in the object store.

        Parameters
        ----------
        data : bytes
            The binary content to store.
        filename : str
            Original filename to record in metadata.

        Returns
        -------
        ObjectRef
            Reference to the stored object.
        """
        md5 = hashlib.md5(data, usedforsecurity=False).hexdigest()
        sha256 = hashlib.sha256(data).hexdigest()
        blob_path = self._blob_path(md5)
        deduplicated = blob_path.exists()

        if not deduplicated:
            self._publish(blob_path, lambda target: target.write(data))
            logger.info("Stored new object: %s (%d bytes)", md5, len(data))
        else:
            if self._compute_sha256_streaming(blob_path) != sha256:
                raise RuntimeError("MD5 collision detected; refusing unsafe deduplication")
            logger.info("Deduplicated object: %s (refcount++)", md5)

        object_uuid = str(uuid.uuid4())
        storage_path = self._relative_path(md5)

        return ObjectRef(
            uuid=object_uuid,
            md5=md5,
            sha256=sha256,
            size=len(data),
            original_filename=filename,
            deduplicated=deduplicated,
            storage_path=storage_path,
        )

    def get(self, md5: str, *, sha256: str | None = None) -> bytes:
        """Retrieve blob content by MD5 hash.

        Parameters
        ----------
        md5 : str
            The MD5 hex digest of the content.

        Returns
        -------
        bytes
            The stored content.

        Raises
        ------
        FileNotFoundError
            If no blob with the given MD5 exists.
        """
        digest = _validate_md5(md5)
        blob_path = self._blob_path(digest)
        if not blob_path.exists():
            raise FileNotFoundError(f"Object not found: {digest}")
        data = blob_path.read_bytes()
        if hashlib.md5(data, usedforsecurity=False).hexdigest() != digest:
            raise OSError("Local object-store content-address verification failed")
        if sha256 is not None and hashlib.sha256(data).hexdigest() != _validate_sha256(sha256):
            raise OSError("Local object-store SHA-256 integrity verification failed")
        return data

    def get_path(self, md5: str, *, sha256: str | None = None) -> Path:
        """Return the filesystem path for a blob.

        Parameters
        ----------
        md5 : str
            The MD5 hex digest of the content.

        Returns
        -------
        Path
            Path to the stored blob.

        Raises
        ------
        FileNotFoundError
            If no blob with the given MD5 exists.
        """
        digest = _validate_md5(md5)
        blob_path = self._blob_path(digest)
        if not blob_path.exists():
            raise FileNotFoundError(f"Object not found: {digest}")
        actual_md5 = self._compute_md5_streaming(blob_path)
        if actual_md5 != digest:
            raise OSError("Local object-store content-address verification failed")
        if sha256 is not None:
            expected_sha256 = _validate_sha256(sha256)
            if self._compute_sha256_streaming(blob_path) != expected_sha256:
                raise OSError("Local object-store SHA-256 integrity verification failed")
        return blob_path

    def materialize(
        self,
        md5: str,
        destination: Path,
        *,
        sha256: str | None = None,
    ) -> Path:
        """Atomically copy a blob to a caller-owned local path."""
        digest = _validate_md5(md5)
        expected_sha256 = _validate_sha256(sha256) if sha256 is not None else None
        source = self._blob_path(digest)
        if not source.exists():
            raise FileNotFoundError(f"Object not found: {digest}")
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
        )
        tmp_path = Path(tmp_name)
        try:
            md5_hasher = hashlib.md5(usedforsecurity=False)
            sha256_hasher = hashlib.sha256()
            with os.fdopen(fd, "wb") as target:
                with source.open("rb") as source_handle:
                    while chunk := source_handle.read(self.chunk_size):
                        md5_hasher.update(chunk)
                        sha256_hasher.update(chunk)
                        target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
            if md5_hasher.hexdigest() != digest or (
                expected_sha256 is not None and sha256_hasher.hexdigest() != expected_sha256
            ):
                raise OSError("Local object-store materialization integrity check failed")
            os.replace(tmp_path, destination)
        finally:
            tmp_path.unlink(missing_ok=True)
        return destination

    def exists(self, md5: str) -> bool:
        """Check if a blob with the given MD5 exists."""
        return self._blob_path(md5).exists()

    def delete(self, md5: str) -> bool:
        """Delete a blob from storage.

        Only deletes if the reference count has reached zero.

        Parameters
        ----------
        md5 : str
            The MD5 hex digest of the content.

        Returns
        -------
        bool
            True if the blob was deleted, False if it still has references.
        """
        blob_path = self._blob_path(md5)
        if blob_path.exists():
            blob_path.unlink()
            logger.info("Deleted object: %s", md5)
            return True
        return False

    def _compute_sha256_streaming(self, path: Path) -> str:
        """Return the SHA-256 digest for an already-opened/staged blob."""
        hasher = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(self.chunk_size):
                hasher.update(chunk)
        return hasher.hexdigest()

    def verify(self, md5: str, sha256: str) -> bool:
        """Verify both the address and strong digest of a stored blob."""
        digest = _validate_md5(md5)
        expected_sha256 = _validate_sha256(sha256)
        path = self._blob_path(digest)
        if not path.exists():
            return False
        md5_hasher = hashlib.md5(usedforsecurity=False)
        sha256_hasher = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(self.chunk_size):
                md5_hasher.update(chunk)
                sha256_hasher.update(chunk)
        return md5_hasher.hexdigest() == digest and sha256_hasher.hexdigest() == expected_sha256


class S3ObjectStore:
    """S3-compatible content-addressed storage with verified local caching.

    Credentials are deliberately absent from this API. When ``client`` is not
    supplied, boto3 resolves credentials through its standard environment,
    shared-config, web-identity, container, or instance-role provider chain.
    This keeps secrets out of MMFDB configuration and database records.
    """

    _CONDITIONAL_CONFLICTS = {
        "409",
        "412",
        "ConditionalRequestConflict",
        "PreconditionFailed",
    }
    _NOT_FOUND = {"404", "NoSuchKey", "NotFound"}
    _MAX_SINGLE_PUT_BYTES = 5 * 1024**3

    def __init__(
        self,
        *,
        bucket: str,
        cache_root: Path,
        prefix: str = "",
        endpoint_url: str | None = None,
        region_name: str | None = None,
        client: Any | None = None,
        chunk_size: int = 65536,
    ):
        bucket = bucket.strip() if isinstance(bucket, str) else ""
        if not bucket or "/" in bucket or "\\" in bucket:
            raise ValueError("S3 object-store bucket must be a non-empty bucket name")
        self.bucket = bucket
        self.prefix = self._normalize_prefix(prefix)
        self.endpoint_url = self._normalize_endpoint_url(endpoint_url)
        self.region_name = region_name
        self.cache_root = Path(cache_root)
        self.cache_root.mkdir(parents=True, exist_ok=True)
        self.chunk_size = max(1, int(chunk_size))
        self._client = client if client is not None else self._create_client()

    @staticmethod
    def _normalize_prefix(prefix: str) -> str:
        if not isinstance(prefix, str):
            raise TypeError("S3 object-store prefix must be a string")
        normalized = prefix.strip().strip("/")
        parts = normalized.split("/") if normalized else []
        if "\\" in normalized or any(part in {"", ".", ".."} for part in parts):
            raise ValueError("S3 object-store prefix must contain plain key segments")
        return "/".join(parts)

    @staticmethod
    def _normalize_endpoint_url(endpoint_url: str | None) -> str | None:
        if endpoint_url is None:
            return None
        value = endpoint_url.strip()
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "S3 endpoint must be an HTTP(S) origin without credentials, "
                "query parameters, or fragments"
            )
        return value.rstrip("/")

    def _create_client(self) -> Any:
        try:
            import boto3
        except ImportError as exc:
            raise RuntimeError(
                "The S3 object-store backend requires boto3. Install boto3 in "
                "the MMFDB runtime; credentials remain managed by the standard "
                "AWS provider chain."
            ) from exc
        return boto3.client(
            "s3",
            endpoint_url=self.endpoint_url,
            region_name=self.region_name,
        )

    @staticmethod
    def _error_code(exc: Exception) -> str | None:
        response = getattr(exc, "response", None)
        if not isinstance(response, dict):
            return None
        error = response.get("Error")
        if not isinstance(error, dict):
            return None
        code = error.get("Code")
        return str(code) if code is not None else None

    def key_for(self, md5: str) -> str:
        """Return the backend key for a validated content address."""
        relative = _relative_address(md5)
        return f"{self.prefix}/{relative}" if self.prefix else relative

    def cache_path(self, md5: str) -> Path:
        """Return the private verified-cache path for a content address."""
        path = self.cache_root / _relative_address(md5)
        try:
            path.resolve().relative_to(self.cache_root.resolve())
        except ValueError as exc:
            raise ValueError("Content address escapes the S3 cache root") from exc
        return path

    def _storage_path(self, md5: str) -> str:
        return f"s3://{self.bucket}/{self.key_for(md5)}"

    def _head(self, md5: str) -> dict[str, Any]:
        digest = _validate_md5(md5)
        try:
            return self._client.head_object(
                Bucket=self.bucket,
                Key=self.key_for(digest),
            )
        except Exception as exc:
            if self._error_code(exc) in self._NOT_FOUND:
                raise FileNotFoundError(f"Object not found: {digest}") from exc
            raise

    @staticmethod
    def _metadata_sha256(response: dict[str, Any]) -> str:
        metadata = response.get("Metadata") or {}
        sha256 = metadata.get("sha256") if isinstance(metadata, dict) else None
        if not isinstance(sha256, str):
            raise OSError("S3 object has no valid SHA-256 integrity metadata")
        try:
            return _validate_sha256(sha256)
        except ValueError as exc:
            raise OSError("S3 object has no valid SHA-256 integrity metadata") from exc

    def _read_verified(
        self,
        md5: str,
        target: BinaryIO | None = None,
        *,
        expected_sha256: str | None = None,
    ) -> tuple[int, str]:
        digest = _validate_md5(md5)
        expected = _validate_sha256(expected_sha256) if expected_sha256 else None
        try:
            response = self._client.get_object(
                Bucket=self.bucket,
                Key=self.key_for(digest),
            )
        except Exception as exc:
            if self._error_code(exc) in self._NOT_FOUND:
                raise FileNotFoundError(f"Object not found: {digest}") from exc
            raise

        body = response.get("Body")
        if body is None or not callable(getattr(body, "read", None)):
            raise OSError("S3 response did not contain a readable payload")
        try:
            metadata_sha256 = self._metadata_sha256(response)
        except Exception:
            close = getattr(body, "close", None)
            if callable(close):
                close()
            raise
        if expected is not None and metadata_sha256 != expected:
            close = getattr(body, "close", None)
            if callable(close):
                close()
            raise OSError("S3 object integrity metadata does not match MMFDB")
        md5_hasher = hashlib.md5(usedforsecurity=False)
        sha256_hasher = hashlib.sha256()
        size = 0
        try:
            while chunk := body.read(self.chunk_size):
                if not isinstance(chunk, bytes):
                    raise OSError("S3 payload stream returned non-byte content")
                md5_hasher.update(chunk)
                sha256_hasher.update(chunk)
                if target is not None:
                    target.write(chunk)
                size += len(chunk)
        finally:
            close = getattr(body, "close", None)
            if callable(close):
                close()

        actual_sha256 = sha256_hasher.hexdigest()
        declared_size = response.get("ContentLength")
        if (
            md5_hasher.hexdigest() != digest
            or actual_sha256 != metadata_sha256
            or (declared_size is not None and int(declared_size) != size)
        ):
            raise OSError("S3 object integrity verification failed")
        return size, actual_sha256

    def _put_stream(
        self,
        source: BinaryIO,
        *,
        md5: str,
        sha256: str,
        size: int,
    ) -> bool:
        digest = _validate_md5(md5)
        strong_digest = _validate_sha256(sha256)
        if size < 0 or size > self._MAX_SINGLE_PUT_BYTES:
            raise ValueError(
                "S3 payload size must be between 0 and 5 GiB; multipart object "
                "publishing is not yet supported"
            )
        try:
            self._client.put_object(
                Bucket=self.bucket,
                Key=self.key_for(digest),
                Body=source,
                ContentLength=size,
                IfNoneMatch="*",
                Metadata={"md5": digest, "sha256": strong_digest},
            )
            deduplicated = False
        except Exception as exc:
            if self._error_code(exc) not in self._CONDITIONAL_CONFLICTS:
                raise
            deduplicated = True

        if not self.verify(digest, strong_digest):
            if not deduplicated:
                try:
                    self._client.delete_object(
                        Bucket=self.bucket,
                        Key=self.key_for(digest),
                    )
                except Exception:
                    logger.exception("Could not remove failed S3 object upload %s", digest)
            raise RuntimeError(
                "Content-address collision or S3 upload corruption detected; "
                "refusing unsafe deduplication"
            )
        return deduplicated

    def put_from_path(
        self,
        path: Path,
        original_filename: str | None = None,
    ) -> ObjectRef:
        """Snapshot, hash, and conditionally upload a local file."""
        path = Path(path)
        if not path.is_file():
            raise FileNotFoundError(f"Source file not found: {path}")

        incoming = self.cache_root / ".incoming"
        incoming.mkdir(parents=True, exist_ok=True)
        fd, staged_name = tempfile.mkstemp(dir=incoming, prefix="blob-", suffix=".tmp")
        staged = Path(staged_name)
        md5_hasher = hashlib.md5(usedforsecurity=False)
        sha256_hasher = hashlib.sha256()
        size = 0
        try:
            with os.fdopen(fd, "wb") as target:
                with path.open("rb") as source:
                    while chunk := source.read(self.chunk_size):
                        md5_hasher.update(chunk)
                        sha256_hasher.update(chunk)
                        target.write(chunk)
                        size += len(chunk)
                target.flush()
                os.fsync(target.fileno())
            md5 = md5_hasher.hexdigest()
            sha256 = sha256_hasher.hexdigest()
            with staged.open("rb") as source:
                deduplicated = self._put_stream(
                    source,
                    md5=md5,
                    sha256=sha256,
                    size=size,
                )
        finally:
            staged.unlink(missing_ok=True)

        return ObjectRef(
            uuid=str(uuid.uuid4()),
            md5=md5,
            sha256=sha256,
            size=size,
            original_filename=original_filename or path.name,
            deduplicated=deduplicated,
            storage_path=self._storage_path(md5),
        )

    def put_bytes(self, data: bytes, filename: str) -> ObjectRef:
        """Conditionally upload an immutable in-memory payload."""
        if not isinstance(data, bytes):
            raise TypeError("Object payload must be bytes")
        md5 = hashlib.md5(data, usedforsecurity=False).hexdigest()
        sha256 = hashlib.sha256(data).hexdigest()
        deduplicated = self._put_stream(
            io.BytesIO(data),
            md5=md5,
            sha256=sha256,
            size=len(data),
        )
        return ObjectRef(
            uuid=str(uuid.uuid4()),
            md5=md5,
            sha256=sha256,
            size=len(data),
            original_filename=filename,
            deduplicated=deduplicated,
            storage_path=self._storage_path(md5),
        )

    def get(self, md5: str, *, sha256: str | None = None) -> bytes:
        """Stream and verify a remote payload into memory."""
        target = io.BytesIO()
        self._read_verified(md5, target, expected_sha256=sha256)
        return target.getvalue()

    @staticmethod
    def _hash_path(path: Path, chunk_size: int) -> tuple[str, str]:
        md5_hasher = hashlib.md5(usedforsecurity=False)
        sha256_hasher = hashlib.sha256()
        with path.open("rb") as source:
            while chunk := source.read(chunk_size):
                md5_hasher.update(chunk)
                sha256_hasher.update(chunk)
        return md5_hasher.hexdigest(), sha256_hasher.hexdigest()

    def get_path(self, md5: str, *, sha256: str | None = None) -> Path:
        """Return a verified local cache path for a remote payload."""
        digest = _validate_md5(md5)
        expected_sha256 = _validate_sha256(sha256) if sha256 is not None else None
        cache_path = self.cache_path(digest)
        remote_sha256 = self._metadata_sha256(self._head(digest))
        if expected_sha256 is not None and remote_sha256 != expected_sha256:
            raise OSError("S3 object integrity metadata does not match MMFDB")
        if cache_path.is_file():
            local_md5, local_sha256 = self._hash_path(cache_path, self.chunk_size)
            if local_md5 == digest and local_sha256 == remote_sha256:
                return cache_path
            cache_path.unlink()
        self.materialize(digest, cache_path, sha256=expected_sha256)
        return cache_path

    def materialize(
        self,
        md5: str,
        destination: Path,
        *,
        sha256: str | None = None,
    ) -> Path:
        """Atomically download and verify a remote payload at ``destination``."""
        digest = _validate_md5(md5)
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as target:
                self._read_verified(digest, target, expected_sha256=sha256)
                target.flush()
                os.fsync(target.fileno())
            os.replace(tmp_path, destination)
        finally:
            tmp_path.unlink(missing_ok=True)
        return destination

    def exists(self, md5: str) -> bool:
        """Return whether the remote content address exists."""
        try:
            self._head(md5)
        except FileNotFoundError:
            return False
        return True

    def delete(self, md5: str) -> bool:
        """Delete a remote payload and its local cache entry."""
        digest = _validate_md5(md5)
        if not self.exists(digest):
            return False
        self._client.delete_object(Bucket=self.bucket, Key=self.key_for(digest))
        self.cache_path(digest).unlink(missing_ok=True)
        logger.info("Deleted S3 object: s3://%s/%s", self.bucket, self.key_for(digest))
        return True

    def verify(self, md5: str, sha256: str) -> bool:
        """Stream the object and verify both MD5 address and SHA-256 digest."""
        digest = _validate_md5(md5)
        strong_digest = _validate_sha256(sha256)
        try:
            _, actual_sha256 = self._read_verified(
                digest,
                expected_sha256=strong_digest,
            )
        except (FileNotFoundError, OSError):
            return False
        return actual_sha256 == strong_digest


def create_object_store(*, s3_client: Any | None = None) -> ObjectStoreBackend:
    """Build the configured object-store backend.

    ``s3_client`` is an explicit injection seam for tests and embedded hosts;
    production callers normally leave it unset so boto3 uses the standard AWS
    credential provider chain.
    """
    from mmfdb.config import (
        configured_object_store_backend,
        configured_s3_bucket,
        configured_s3_endpoint_url,
        configured_s3_prefix,
        configured_s3_region,
        configured_settings_dir,
    )
    from mmfdb.store.database_resolver import object_store_root

    backend = configured_object_store_backend()
    if backend == "local":
        return ObjectStore(object_store_root())
    if backend != "s3":
        raise ValueError(
            f"Unsupported MMFDB object-store backend {backend!r}; expected 'local' or 's3'"
        )
    bucket = configured_s3_bucket()
    if not bucket:
        raise ValueError("An S3 bucket (MMFDB_S3_BUCKET) is required for this backend")
    endpoint = S3ObjectStore._normalize_endpoint_url(configured_s3_endpoint_url())
    prefix = configured_s3_prefix()
    namespace = hashlib.sha256(f"{endpoint or 'aws'}\0{bucket}\0{prefix}".encode()).hexdigest()[:16]
    return S3ObjectStore(
        bucket=bucket,
        prefix=prefix,
        endpoint_url=endpoint,
        region_name=configured_s3_region(),
        cache_root=configured_settings_dir() / "object-cache" / namespace,
        client=s3_client,
    )
