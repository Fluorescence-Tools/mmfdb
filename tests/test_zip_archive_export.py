"""Deposition-bundle contract for ``archive.zip.export``.

The bundler is the only writer of a deposition ZIP, so the mapping from
provenance node to bundled member has to be injective: a reader restores each
node's payload by following its manifest entry, and two nodes silently sharing
one member restores the wrong bytes without any error.
"""

from __future__ import annotations

import json
import pathlib
import zipfile
from unittest.mock import patch

import pytest

from mmfdb.admin.backend.measurement_services import export_zip_archive_handler
from mmfdb.config import configure_runtime, reset_runtime_config
from mmfdb.repository import MFDatabase


@pytest.fixture
def bundle_env(tmp_path: pathlib.Path):
    """Resolve the database and object store into a throwaway directory."""
    db_path = tmp_path / "archive_test.db"
    configure_runtime(settings_dir=tmp_path, database_path=db_path)
    patcher = patch(
        "mmfdb.admin.backend.measurement_services.resolve_database_path",
        return_value=db_path,
    )
    patcher.start()
    try:
        yield db_path
    finally:
        patcher.stop()
        reset_runtime_config()


def _link(db: MFDatabase, operation_id: str, artifact_ids: list[str]) -> None:
    """Attach artifacts to one operation so they share a provenance graph."""
    db.add_operation(operation_id, operation_type="burst_selection")
    for index, artifact_id in enumerate(artifact_ids):
        db.add_operation_artifact(
            operation_id,
            artifact_id,
            role="input" if index == 0 else "output",
            direction="input" if index == 0 else "output",
        )


def _manifest(zip_path: pathlib.Path) -> dict:
    with zipfile.ZipFile(zip_path) as zf:
        return json.loads(zf.read("manifest.json"))


def _payload_entries(manifest: dict) -> dict[str, dict]:
    return {
        entry["node_id"]: entry
        for entry in manifest["files"]
        if entry.get("node_id")
    }


def test_same_basename_artifacts_get_distinct_bundle_members(
    bundle_env: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """Two artifacts named ``data.txt`` are bundled as two distinct members."""
    first_dir = tmp_path / "run_a"
    second_dir = tmp_path / "run_b"
    first_dir.mkdir()
    second_dir.mkdir()
    (first_dir / "data.txt").write_bytes(b"first payload")
    (second_dir / "data.txt").write_bytes(b"second payload")

    with MFDatabase(bundle_env) as db:
        db.register_artifact("art_a", file_path=str(first_dir / "data.txt"))
        db.register_artifact("art_b", file_path=str(second_dir / "data.txt"))
        _link(db, "op_1", ["art_a", "art_b"])

    zip_path = tmp_path / "bundle.zip"
    result = export_zip_archive_handler(
        target_zip_path=str(zip_path),
        seed_node_type="artifact",
        seed_node_id="art_a",
        include_external_data=True,
    )
    assert result["ok"] is True

    entries = _payload_entries(_manifest(zip_path))
    assert entries["art_a"]["copied"] is True
    assert entries["art_b"]["copied"] is True
    assert entries["art_a"]["relative_path"] != entries["art_b"]["relative_path"]

    with zipfile.ZipFile(zip_path) as zf:
        members = [n for n in zf.namelist() if n.startswith("external_data/")]
        assert len(members) == len(set(members)) == 2
        assert zf.read(entries["art_a"]["relative_path"]) == b"first payload"
        assert zf.read(entries["art_b"]["relative_path"]) == b"second payload"


def test_artifacts_sharing_one_file_are_bundled_once(
    bundle_env: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """Two nodes pointing at the same file share a single bundled copy."""
    shared = tmp_path / "shared.txt"
    shared.write_bytes(b"shared payload")

    with MFDatabase(bundle_env) as db:
        db.register_artifact("art_a", file_path=str(shared))
        db.register_artifact("art_b", file_path=str(shared))
        _link(db, "op_1", ["art_a", "art_b"])

    zip_path = tmp_path / "bundle.zip"
    export_zip_archive_handler(
        target_zip_path=str(zip_path),
        seed_node_type="artifact",
        seed_node_id="art_a",
        include_external_data=True,
    )

    entries = _payload_entries(_manifest(zip_path))
    assert entries["art_a"]["relative_path"] == entries["art_b"]["relative_path"]
    with zipfile.ZipFile(zip_path) as zf:
        members = [n for n in zf.namelist() if n.startswith("external_data/")]
        assert members == [entries["art_a"]["relative_path"]]


def test_object_store_blob_is_bundled_when_the_recorded_path_is_gone(
    bundle_env: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """An ingested artifact bundles from the object store, not its stale path."""
    source = tmp_path / "ingested.ptu"
    source.write_bytes(b"ingested payload")

    with MFDatabase(bundle_env) as db:
        ref = db.put_object(path=source, filename="ingested.ptu")
        db.register_artifact(
            "art_stored",
            file_path=str(source),
            object_uuid=ref["object_uuid"],
        )
        _link(db, "op_1", ["art_stored"])

    source.unlink()  # the recorded path no longer resolves

    zip_path = tmp_path / "bundle.zip"
    export_zip_archive_handler(
        target_zip_path=str(zip_path),
        seed_node_type="artifact",
        seed_node_id="art_stored",
        include_external_data=True,
    )

    entry = _payload_entries(_manifest(zip_path))["art_stored"]
    assert entry["source"] == "object_store"
    assert entry["copied"] is True
    with zipfile.ZipFile(zip_path) as zf:
        assert zf.read(entry["relative_path"]) == b"ingested payload"


def test_object_backed_artifact_without_a_file_path_is_still_bundled(
    bundle_env: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """A node whose only location is the object store is not skipped."""
    with MFDatabase(bundle_env) as db:
        ref = db.put_object(data=b"blob only", filename="blob_only.dat")
        db.register_artifact(
            "art_blob",
            storage_mode="embedded_blob",
            object_uuid=ref["object_uuid"],
        )
        _link(db, "op_1", ["art_blob"])

    zip_path = tmp_path / "bundle.zip"
    export_zip_archive_handler(
        target_zip_path=str(zip_path),
        seed_node_type="artifact",
        seed_node_id="art_blob",
        include_external_data=True,
    )

    entry = _payload_entries(_manifest(zip_path))["art_blob"]
    assert entry["copied"] is True
    with zipfile.ZipFile(zip_path) as zf:
        assert zf.read(entry["relative_path"]) == b"blob only"


def test_unreachable_payload_states_why_in_the_manifest(
    bundle_env: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """A missing file leaves a stated reason rather than a silent gap."""
    with MFDatabase(bundle_env) as db:
        db.register_artifact("art_missing", file_path=str(tmp_path / "never_written.txt"))
        _link(db, "op_1", ["art_missing"])

    zip_path = tmp_path / "bundle.zip"
    export_zip_archive_handler(
        target_zip_path=str(zip_path),
        seed_node_type="artifact",
        seed_node_id="art_missing",
        include_external_data=True,
    )

    entry = _payload_entries(_manifest(zip_path))["art_missing"]
    assert entry["copied"] is False
    assert "never_written.txt" in entry["unavailable_reason"]


def test_bundle_without_external_data_carries_no_payload_members(
    bundle_env: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """The snapshot-only bundle still records the nodes it did not copy."""
    payload = tmp_path / "data.txt"
    payload.write_bytes(b"payload")

    with MFDatabase(bundle_env) as db:
        db.register_artifact("art_a", file_path=str(payload))
        _link(db, "op_1", ["art_a"])

    zip_path = tmp_path / "bundle.zip"
    export_zip_archive_handler(
        target_zip_path=str(zip_path),
        seed_node_type="artifact",
        seed_node_id="art_a",
        include_external_data=False,
    )

    entry = _payload_entries(_manifest(zip_path))["art_a"]
    assert entry["copied"] is False
    with zipfile.ZipFile(zip_path) as zf:
        assert not [n for n in zf.namelist() if n.startswith("external_data/")]
        assert set(zf.namelist()) == {
            "manifest.json",
            "database_snapshot.db",
            "provenance_graph.json",
        }
