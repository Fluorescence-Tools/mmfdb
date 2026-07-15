from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path

import pytest

from mmfdb.config import configure_runtime, reset_runtime_config
from mmfdb.repository import MFDatabase
from mmfdb.store import database_resolver
from mmfdb.store.object_store import ObjectStore


def test_object_store_root_defaults_to_settings_objects(tmp_path):
    """Object store root defaults to the settings directory objects folder."""
    configure_runtime(settings_dir=tmp_path)
    try:
        assert database_resolver.object_store_root() == tmp_path / "objects"
    finally:
        reset_runtime_config()


def test_object_store_root_uses_relative_mmfdb_setting(tmp_path):
    """Relative MMFDB object store settings are resolved below settings."""
    configure_runtime(settings_dir=tmp_path, object_store_root="custom_objects")
    try:
        assert database_resolver.object_store_root() == tmp_path / "custom_objects"
    finally:
        reset_runtime_config()


def test_object_store_root_uses_absolute_mmfdb_setting(tmp_path):
    """Absolute MMFDB object store settings are used as-is."""
    custom_root = tmp_path / "external" / "objects"
    configure_runtime(settings_dir=tmp_path, object_store_root=custom_root)
    try:
        assert database_resolver.object_store_root() == custom_root
    finally:
        reset_runtime_config()


def test_object_store_deduplicates_identical_files(tmp_path):
    """Identical files are stored once under the same content-addressed path."""
    store = ObjectStore(tmp_path)
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_bytes(b"same content")
    second.write_bytes(b"same content")

    first_ref = store.put_from_path(first)
    second_ref = store.put_from_path(second)

    assert first_ref.md5 == second_ref.md5
    assert first_ref.storage_path == second_ref.storage_path
    assert second_ref.deduplicated is True
    assert (tmp_path / first_ref.storage_path).read_bytes() == b"same content"


@pytest.mark.parametrize(
    "digest",
    ["../outside", "/etc/passwd", "a" * 31, "g" * 32, "A" * 32, "a/b" + "0" * 29],
)
def test_object_store_rejects_invalid_content_addresses(tmp_path, digest):
    """A digest is a value object, never a caller-controlled filesystem path."""
    store = ObjectStore(tmp_path / "objects")
    with pytest.raises(ValueError, match="MD5"):
        store.get(digest)
    with pytest.raises(ValueError, match="MD5"):
        store.delete(digest)


def test_object_store_publishes_blobs_with_atomic_replace(tmp_path, monkeypatch):
    """Readers must never observe a partially copied final blob."""
    root = tmp_path / "objects"
    source = tmp_path / "payload.bin"
    source.write_bytes(os.urandom(128 * 1024))
    expected = hashlib.md5(source.read_bytes()).hexdigest()
    replace_calls: list[tuple[str, str]] = []
    real_replace = os.replace

    def recording_replace(src, dst):
        replace_calls.append((str(src), str(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", recording_replace)
    ref = ObjectStore(root).put_from_path(source)

    assert ref.md5 == expected
    assert replace_calls
    assert Path(replace_calls[-1][1]) == root / ref.storage_path
    assert not list(root.rglob("*.tmp"))


def test_object_store_bytes_are_published_atomically(tmp_path, monkeypatch):
    root = tmp_path / "objects"
    calls: list[tuple[str, str]] = []
    real_replace = os.replace

    def recording_replace(src, dst):
        calls.append((str(src), str(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", recording_replace)
    ref = ObjectStore(root).put_bytes(b"atomic", "atomic.bin")

    assert calls and Path(calls[-1][1]) == root / ref.storage_path
    assert (root / ref.storage_path).read_bytes() == b"atomic"


def test_failed_object_registration_removes_new_orphan_blob(tmp_path, monkeypatch):
    """A database/audit rollback must not strand a newly published payload."""
    db = MFDatabase(str(tmp_path / "mmfdb.sqlite"))
    db._object_store = ObjectStore(tmp_path / "objects")
    monkeypatch.setattr(
        db,
        "add_audit_log",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("audit failed")),
    )
    try:
        with pytest.raises(RuntimeError, match="audit failed"):
            db.put_object(data=b"orphan", filename="orphan.bin")
        digest = hashlib.md5(b"orphan").hexdigest()
        with pytest.raises(FileNotFoundError):
            db._object_store.get_path(digest)
        assert db.conn.execute(
            "SELECT COUNT(*) FROM mmfdb_object WHERE content_md5 = ?", (digest,)
        ).fetchone()[0] == 0
    finally:
        db.close()


def test_referenced_object_cannot_be_deleted_out_from_under_artifact(tmp_path):
    db = MFDatabase(str(tmp_path / "mmfdb.sqlite"))
    db._object_store = ObjectStore(tmp_path / "objects")
    try:
        stored = db.put_object(data=b"protected", filename="protected.bin")
        db.register_artifact(
            artifact_id="artifact-protected",
            artifact_kind="processed_data",
            storage_mode="local_file",
            object_uuid=stored["object_uuid"],
        )
        with pytest.raises(ValueError, match="referenced by 1 live artifact"):
            db.delete_object(stored["object_uuid"])
        assert db.get_object(stored["object_uuid"]) == b"protected"
    finally:
        db.close()


def test_outer_transaction_rollback_compensates_published_blob(tmp_path):
    """Multi-output workflows must not leak blobs when later work rolls back."""
    db = MFDatabase(str(tmp_path / "mmfdb.sqlite"))
    db._object_store = ObjectStore(tmp_path / "objects")
    digest = hashlib.md5(b"first output").hexdigest()
    try:
        with pytest.raises(RuntimeError, match="second output failed"):
            with db.transaction():
                db.put_object(data=b"first output", filename="first.bin")
                raise RuntimeError("second output failed")
        assert db.conn.execute(
            "SELECT 1 FROM mmfdb_object WHERE content_md5 = ?", (digest,)
        ).fetchone() is None
        with pytest.raises(FileNotFoundError):
            db._object_store.get_path(digest)
    finally:
        db.close()


def test_outer_transaction_rollback_does_not_delete_committed_blob(tmp_path):
    """Filesystem GC must wait for the outermost SQL commit."""
    db = MFDatabase(str(tmp_path / "mmfdb.sqlite"))
    db._object_store = ObjectStore(tmp_path / "objects")
    try:
        stored = db.put_object(data=b"keep on rollback", filename="keep.bin")
        with pytest.raises(RuntimeError, match="abort outer workflow"):
            with db.transaction():
                result = db.delete_object(stored["object_uuid"])
                assert result["blob_gc_scheduled"] is True
                raise RuntimeError("abort outer workflow")

        assert db.get_object(stored["object_uuid"]) == b"keep on rollback"
    finally:
        db.close()


def test_project_archive_handler_roundtrips_csp_object(tmp_path, monkeypatch):
    """MMFDB project archival stores and restores the complete CSP object."""
    from mmfdb.admin.backend import measurement_services

    db_path = tmp_path / "mmfdb.sqlite"
    object_root = tmp_path / "objects"
    monkeypatch.setattr(measurement_services, "resolve_database_path", lambda: db_path)
    monkeypatch.setattr(database_resolver, "object_store_root", lambda: object_root)

    archive_bytes = b"PK\x03\x04fake-csp-archive"
    payload = {
        "project_format_version": 4,
        "meta": {"name": "ProteinMC project"},
        "datasets": {},
        "fits": [],
        "ui": {},
        "extra": {},
    }

    archived = measurement_services.archive_project_handler(
        project_id="proj-proteinmc",
        project_name="ProteinMC project",
        project_payload=payload,
        project_archive_data=base64.b64encode(archive_bytes).decode("ascii"),
        project_archive_filename="proteinmc.csp",
    )

    assert archived["ok"] is True
    assert archived["archive_object"]["object_uuid"]

    restored = measurement_services.restore_project_handler("proj-proteinmc")

    assert restored["ok"] is True
    assert base64.b64decode(restored["project_archive_data"]) == archive_bytes
    assert restored["project_archive"]["object_uuid"] == archived["archive_object"]["object_uuid"]
