"""The real admin seed works from its installed data, never an application test tree."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import sys
from pathlib import Path

import pytest
from mmfdb.admin.backend import services
from mmfdb.admin.seed_example import DEMO, seed_example
from mmfdb.config import configure_runtime, reset_runtime_config
from mmfdb.repository import MFDatabase
from mmfdb.security.auth import create_session


@pytest.fixture
def seed_backend(tmp_path, monkeypatch):
    """Bind auth, demo data and objects to scratch paths and forbid home access."""
    expected_package = os.environ.get("MMFDB_TEST_PACKAGE_ROOT")
    if expected_package:
        assert Path(seed_example.__code__.co_filename).is_relative_to(Path(expected_package))
        assert Path(services.__file__).is_relative_to(Path(expected_package))
    monkeypatch.setitem(sys.modules, "chisurf", None)
    target = tmp_path / "authenticated.sqlite"
    default = tmp_path / "unrelated.sqlite"
    configure_runtime(
        settings_dir=tmp_path / "settings",
        database_path=default,
        source_database_path=tmp_path / "missing.db",
        object_store_root=tmp_path / "objects",
    )
    with MFDatabase(target) as db:
        db.add_user("scratch_actor", "Scratch actor")
        auth = {"token": create_session(db.conn, "scratch_actor")["token"]}
        db.conn.commit()
    with MFDatabase(default) as db:
        db.add_sample("unrelated", description="untouched default database")
    unchanged = default.read_bytes()
    monkeypatch.setattr(services, "_resolved_db_path", str(target))

    def forbidden_home():
        """No seed result is stored at an unrelated user's home directory."""
        raise AssertionError("seed accessed the user's home")

    monkeypatch.setattr(Path, "home", forbidden_home)
    try:
        yield target, default, auth, unchanged
    finally:
        reset_runtime_config()


def _seed(seed_backend):
    """Invoke the actual authenticated backend action."""
    return services.populate_mock_data_handler(auth=seed_backend[2])["summary"]


def _counts(db):
    """Capture logical records and object references to prove repeat idempotence."""
    return {
        table: db.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("mmfdb_artifact", "mmfdb_operation", "mmfdb_operation_artifact", "mmfdb_edge")
    }, [
        tuple(row)
        for row in db.conn.execute(
            "SELECT object_uuid, refcount FROM mmfdb_object ORDER BY object_uuid"
        )
    ]


def test_actual_backend_seed_stores_packaged_tables_and_real_selection(seed_backend):
    """Real bytes, row counts and lineage describe a synthetic table selection."""
    target, default, _, unchanged = seed_backend
    summary = _seed(seed_backend)
    assert Path(summary["database_path"]) == target
    assert summary["raw_data_ids"] == [f"raw_demo_sm_dna_{i:03d}" for i in range(3)]
    assert summary["processed_data_id"] == f"prod_{DEMO['processing_id']}"
    assert "warning" not in summary
    assert default.read_bytes() == unchanged
    with MFDatabase(target) as db:
        for artifact_id, count in zip(summary["raw_data_ids"], [50, 38, 32]):
            artifact = db.get_artifact(artifact_id)
            assert artifact["artifact_kind"] == "burst_table" and artifact["data_format"] == "csv"
            assert artifact["object_uuid"] and not artifact["file_path"]
            assert artifact["row_count"] == count
            data = db.get_object(artifact["object_uuid"])
            assert hashlib.sha256(data).hexdigest() == artifact["checksum"]
            assert len(list(csv.DictReader(io.StringIO(data.decode())))) == count
        product = db.get_artifact(summary["processed_data_id"])
        output = db.get_object(product["object_uuid"])
        rows = list(csv.DictReader(io.StringIO(output.decode())))
        assert product["row_count"] == len(rows) == 63
        assert {row["artifact_id"] for row in rows} == set(summary["raw_data_ids"])
        assert all(float(row["proximity_ratio"]) >= 0.5 for row in rows)
        assert set(db.lineage.ancestors(summary["processed_data_id"])) == set(
            summary["raw_data_ids"]
        )
        operation = db.get_operation(DEMO["processing_id"])
        assert operation["operation_type"] == "burst_filtering"
        assert operation["software_module"] == "mmfdb.admin.seed_example"
        assert json.loads(operation["settings_json"])["column"] == "proximity_ratio"
        assert len(summary["quality_example_raw_data_ids"]) == 1


def test_repeat_seed_does_not_duplicate_objects_references_or_lineage(seed_backend):
    """Existing demo IDs retain their bytes and references across repeated actions."""
    first = _seed(seed_backend)
    with MFDatabase(seed_backend[0]) as db:
        before = _counts(db)
    second = _seed(seed_backend)
    assert first == second
    with MFDatabase(seed_backend[0]) as db:
        assert _counts(db) == before


@pytest.mark.parametrize("fault", ["missing", "headers", "nonfinite"])
def test_explicit_invalid_data_dir_is_rejected_before_seeding(seed_backend, tmp_path, fault):
    """Override input validation never seeds a partial or falsely successful demo."""
    directory = tmp_path / "override"
    directory.mkdir()
    for filename in ("bursts.csv", "bursts_rep1.csv", "bursts_rep2.csv"):
        (directory / filename).write_text("burst_id,size,proximity_ratio\n0,30,0.75\n")
    if fault == "missing":
        (directory / "bursts_rep2.csv").unlink()
    elif fault == "headers":
        (directory / "bursts_rep2.csv").write_text("unexpected\n1\n")
    else:
        (directory / "bursts_rep2.csv").write_text("burst_id,size,proximity_ratio\n0,30,nan\n")
    with pytest.raises(ValueError):
        seed_example(seed_backend[0], data_dir=directory)
    with MFDatabase(seed_backend[0]) as db:
        assert db.get_sample(DEMO["sample_id"]) is None
        assert not db.list_artifacts()
    assert not (tmp_path / "objects").exists()


def test_explicit_table_override_is_owned_and_changed_repeat_is_rejected(seed_backend, tmp_path):
    """An explicit demo dataset is stored once, never silently mislabeled on reseed."""
    directory = tmp_path / "override"
    directory.mkdir()
    for filename in ("bursts.csv", "bursts_rep1.csv", "bursts_rep2.csv"):
        (directory / filename).write_text("burst_id,size,proximity_ratio\n0,20,0.25\n1,30,0.75\n")
    first = seed_example(seed_backend[0], data_dir=directory)
    with MFDatabase(seed_backend[0]) as db:
        assert db.get_artifact(first["processed_data_id"])["row_count"] == 3
        before = _counts(db)
    (directory / "bursts_rep2.csv").write_text("burst_id,size,proximity_ratio\n0,20,0.8\n")
    with pytest.raises(ValueError, match="already seeded"):
        seed_example(seed_backend[0], data_dir=directory)
    with MFDatabase(seed_backend[0]) as db:
        assert _counts(db) == before


def test_backend_seed_keeps_authentication_required(seed_backend):
    """The packaging repair never grants an unauthenticated write path."""
    from mmfdb.security.auth import AuthError

    with pytest.raises(AuthError):
        services.populate_mock_data_handler(auth=None)
    with MFDatabase(seed_backend[0]) as db:
        assert db.get_sample(DEMO["sample_id"]) is None
        assert not db.list_artifacts()
