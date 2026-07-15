from __future__ import annotations

import builtins
import hashlib
import io
from pathlib import Path

import pytest
from mmfdb.config import configure_runtime, reset_runtime_config
from mmfdb.repository import MFDatabase
from mmfdb.store.object_store import (
    ObjectStore,
    S3ObjectStore,
    create_object_store,
)


class _S3Error(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class _StreamingBody:
    def __init__(self, data: bytes):
        self._source = io.BytesIO(data)
        self.read_sizes: list[int] = []
        self.closed = False

    def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        return self._source.read(size)

    def close(self) -> None:
        self.closed = True
        self._source.close()


class _FakeS3Client:
    def __init__(self):
        self.objects: dict[tuple[str, str], tuple[bytes, dict[str, str]]] = {}
        self.put_calls: list[dict] = []
        self.get_bodies: list[_StreamingBody] = []

    def put_object(self, **kwargs):
        self.put_calls.append(kwargs.copy())
        identity = (kwargs["Bucket"], kwargs["Key"])
        if kwargs.get("IfNoneMatch") == "*" and identity in self.objects:
            raise _S3Error("PreconditionFailed")
        body = kwargs["Body"]
        chunks: list[bytes] = []
        while chunk := body.read(7):
            chunks.append(chunk)
        self.objects[identity] = (b"".join(chunks), dict(kwargs.get("Metadata", {})))
        return {"ETag": '"fake"'}

    def head_object(self, **kwargs):
        try:
            data, metadata = self.objects[(kwargs["Bucket"], kwargs["Key"])]
        except KeyError as exc:
            raise _S3Error("NoSuchKey") from exc
        return {"ContentLength": len(data), "Metadata": metadata}

    def get_object(self, **kwargs):
        try:
            data, metadata = self.objects[(kwargs["Bucket"], kwargs["Key"])]
        except KeyError as exc:
            raise _S3Error("NoSuchKey") from exc
        body = _StreamingBody(data)
        self.get_bodies.append(body)
        return {"Body": body, "ContentLength": len(data), "Metadata": metadata}

    def delete_object(self, **kwargs):
        self.objects.pop((kwargs["Bucket"], kwargs["Key"]), None)
        return {}


def _store(tmp_path: Path, client: _FakeS3Client) -> S3ObjectStore:
    return S3ObjectStore(
        bucket="research-data",
        prefix="mmfdb/blobs",
        endpoint_url="https://objects.example.test",
        region_name="test-1",
        cache_root=tmp_path / "cache",
        client=client,
        chunk_size=5,
    )


def test_factory_preserves_local_store_default(tmp_path):
    configure_runtime(settings_dir=tmp_path)
    try:
        store = create_object_store()
        assert isinstance(store, ObjectStore)
        assert store.root == tmp_path / "objects"
    finally:
        reset_runtime_config()


def test_factory_builds_s3_store_without_credential_configuration(tmp_path):
    client = _FakeS3Client()
    configure_runtime(
        settings_dir=tmp_path,
        object_store_backend="s3",
        s3_bucket="research-data",
        s3_prefix="tenant-a",
        s3_endpoint_url="https://objects.example.test",
        s3_region="test-1",
    )
    try:
        store = create_object_store(s3_client=client)
        assert isinstance(store, S3ObjectStore)
        assert store.bucket == "research-data"
        assert store.prefix == "tenant-a"
        assert store.endpoint_url == "https://objects.example.test"
        assert not any("secret" in name or "credential" in name for name in vars(store))
    finally:
        reset_runtime_config()


def test_s3_put_is_conditional_streaming_and_deduplicated(tmp_path):
    client = _FakeS3Client()
    store = _store(tmp_path, client)
    payload = b"content addressed payload"

    first = store.put_bytes(payload, "first.bin")
    second = store.put_bytes(payload, "second.bin")

    assert first.deduplicated is False
    assert second.deduplicated is True
    assert first.md5 == hashlib.md5(payload, usedforsecurity=False).hexdigest()
    assert first.sha256 == hashlib.sha256(payload).hexdigest()
    assert (
        first.storage_path
        == f"s3://research-data/mmfdb/blobs/{first.md5[:2]}/{first.md5[2:4]}/{first.md5}"
    )
    assert client.put_calls[0]["IfNoneMatch"] == "*"
    assert client.put_calls[0]["ContentLength"] == len(payload)
    assert client.put_calls[0]["Metadata"]["sha256"] == first.sha256
    assert store.get(first.md5) == payload
    assert store.verify(first.md5, first.sha256)
    assert client.get_bodies[-1].read_sizes[0] == 5


def test_s3_rejects_oversized_single_put_before_contacting_endpoint(tmp_path):
    client = _FakeS3Client()
    store = _store(tmp_path, client)
    empty_md5 = hashlib.md5(b"", usedforsecurity=False).hexdigest()
    empty_sha256 = hashlib.sha256(b"").hexdigest()

    with pytest.raises(ValueError, match="5 GiB"):
        store._put_stream(
            io.BytesIO(),
            md5=empty_md5,
            sha256=empty_sha256,
            size=store._MAX_SINGLE_PUT_BYTES + 1,
        )

    assert client.put_calls == []


def test_s3_path_upload_and_materialization_are_streamed_and_verified(tmp_path):
    client = _FakeS3Client()
    store = _store(tmp_path, client)
    source = tmp_path / "source.dat"
    source.write_bytes(b"0123456789" * 20)

    ref = store.put_from_path(source)
    cached = store.get_path(ref.md5)
    destination = tmp_path / "materialized" / "copy.dat"
    result = store.materialize(ref.md5, destination)

    assert cached.read_bytes() == source.read_bytes()
    assert result == destination
    assert destination.read_bytes() == source.read_bytes()
    assert not list((tmp_path / "cache").rglob("*.tmp"))


def test_s3_corruption_is_never_materialized(tmp_path):
    client = _FakeS3Client()
    store = _store(tmp_path, client)
    ref = store.put_bytes(b"trusted", "trusted.bin")
    key = (store.bucket, store.key_for(ref.md5))
    _, metadata = client.objects[key]
    client.objects[key] = (b"tampered", metadata)

    assert store.verify(ref.md5, ref.sha256) is False
    destination = tmp_path / "existing.bin"
    destination.write_bytes(b"keep this")
    with pytest.raises(OSError, match="integrity"):
        store.materialize(ref.md5, destination, sha256=ref.sha256)
    assert destination.read_bytes() == b"keep this"
    with pytest.raises(OSError, match="integrity"):
        store.get_path(ref.md5)
    assert not store.cache_path(ref.md5).exists()


def test_s3_exists_delete_and_missing_object(tmp_path):
    client = _FakeS3Client()
    store = _store(tmp_path, client)
    ref = store.put_bytes(b"short lived", "short.bin")

    assert store.exists(ref.md5)
    assert store.delete(ref.md5)
    assert not store.exists(ref.md5)
    assert store.delete(ref.md5) is False
    with pytest.raises(FileNotFoundError):
        store.get(ref.md5)


def test_s3_configuration_rejects_missing_bucket(tmp_path):
    configure_runtime(settings_dir=tmp_path, object_store_backend="s3")
    try:
        with pytest.raises(ValueError, match="bucket"):
            create_object_store(s3_client=_FakeS3Client())
    finally:
        reset_runtime_config()


def test_s3_missing_integrity_metadata_closes_response_stream(tmp_path):
    client = _FakeS3Client()
    store = _store(tmp_path, client)
    payload = b"legacy without integrity metadata"
    digest = hashlib.md5(payload, usedforsecurity=False).hexdigest()
    client.objects[(store.bucket, store.key_for(digest))] = (payload, {})

    with pytest.raises(OSError, match="SHA-256"):
        store.get(digest)

    assert client.get_bodies[-1].closed is True


def test_factory_reads_s3_endpoint_configuration_from_environment(tmp_path, monkeypatch):
    client = _FakeS3Client()
    monkeypatch.setenv("MMFDB_OBJECT_STORE_BACKEND", "s3")
    monkeypatch.setenv("MMFDB_S3_BUCKET", "env-bucket")
    monkeypatch.setenv("MMFDB_S3_PREFIX", "/tenant/data/")
    monkeypatch.setenv("MMFDB_S3_ENDPOINT_URL", "https://minio.example.test")
    monkeypatch.setenv("MMFDB_S3_REGION", "local-1")
    configure_runtime(settings_dir=tmp_path)
    try:
        store = create_object_store(s3_client=client)
        assert isinstance(store, S3ObjectStore)
        assert store.bucket == "env-bucket"
        assert store.prefix == "tenant/data"
        assert store.endpoint_url == "https://minio.example.test"
        assert store.region_name == "local-1"
    finally:
        reset_runtime_config()


@pytest.mark.parametrize("prefix", ["a//b", "a/../b", r"a\b"])
def test_s3_rejects_ambiguous_key_prefixes(tmp_path, prefix):
    with pytest.raises(ValueError, match="prefix"):
        S3ObjectStore(
            bucket="research-data",
            prefix=prefix,
            cache_root=tmp_path,
            client=_FakeS3Client(),
        )


@pytest.mark.parametrize(
    "endpoint",
    [
        "ftp://objects.example.test",
        "https://access:secret@objects.example.test",
        "https://objects.example.test?token=secret",
    ],
)
def test_s3_rejects_unsafe_endpoint_urls(tmp_path, endpoint):
    with pytest.raises(ValueError, match="endpoint"):
        S3ObjectStore(
            bucket="research-data",
            endpoint_url=endpoint,
            cache_root=tmp_path,
            client=_FakeS3Client(),
        )


def test_s3_backend_has_clear_error_when_optional_client_is_missing(tmp_path, monkeypatch):
    real_import = builtins.__import__

    def import_without_boto(name, *args, **kwargs):
        if name == "boto3":
            raise ImportError("not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_boto)
    with pytest.raises(RuntimeError, match="requires boto3"):
        S3ObjectStore(bucket="research-data", cache_root=tmp_path)


def test_local_store_rejects_corruption_before_returning_bytes(tmp_path):
    store = ObjectStore(tmp_path / "objects")
    ref = store.put_bytes(b"trusted local", "local.bin")
    (store.root / ref.storage_path).write_bytes(b"tampered local")

    with pytest.raises(OSError, match="content-address"):
        store.get(ref.md5, sha256=ref.sha256)


def test_repository_roundtrips_and_materializes_through_s3_backend(tmp_path):
    client = _FakeS3Client()
    store = _store(tmp_path, client)
    db = MFDatabase(tmp_path / "mmfdb.sqlite")
    db._object_store = store
    try:
        stored = db.put_object(data=b"remote dataset", filename="dataset.bin")
        db.register_artifact(
            artifact_id="remote-artifact",
            artifact_kind="raw_data",
            data_format="bin",
            storage_mode="embedded_blob",
            object_uuid=stored["object_uuid"],
        )

        assert db.get_object(stored["object_uuid"]) == b"remote dataset"
        materialized = Path(db.materialize_artifact_file("remote-artifact", into=tmp_path))
        assert materialized.suffix == ".bin"
        assert materialized.read_bytes() == b"remote dataset"
    finally:
        db.close()
