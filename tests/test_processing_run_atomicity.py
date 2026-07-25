"""Guardrails for the processing-run write path (assessment finding DATA-04).

Two properties are pinned here:

* ``add_processing_run`` is **atomic** — the operation row, its provenance
  edges, and its audit-log entry either all land or none do. A failure part way
  through must not leave an operation stranded without its inputs.
* An MD5 digest is **labelled as MD5**, not silently stored under the generic
  ``checksum`` column with the default SHA-256 algorithm tag.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from mmfdb.repository import MFDatabase


def _raw_artifact(db: MFDatabase, raw_path: Path) -> str:
    """Register one raw-data artifact and return its id."""
    return db.add_raw_data_reference(
        experiment_id="exp_1",
        data_type="PTU",
        storage_mode="local_file",
        file_path=str(raw_path),
        size_bytes=raw_path.stat().st_size,
        checksum="0" * 64,
        validation_status="valid",
    )


def _count(db: MFDatabase, table: str, column: str, value: str) -> int:
    """Count undeleted rows of ``table`` where ``column`` equals ``value``."""
    row = db.conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {column} = ?", (value,)).fetchone()
    return int(row[0])


def test_add_processing_run_rejects_unknown_inputs_without_writing(tmp_path: Path) -> None:
    """A missing input artifact aborts the run before any row is written."""
    raw_path = tmp_path / "input.ptu"
    raw_path.write_bytes(b"fake tttr")

    with MFDatabase(tmp_path / "test.db") as db:
        db.add_sample("sample_1")
        db.add_experiment("exp_1", sample_id="sample_1", status="complete")
        raw_id = _raw_artifact(db, raw_path)

        with pytest.raises(Exception, match="Missing input raw data artifact"):
            db.add_processing_run(
                run_id="proc_missing",
                experiment_id="exp_1",
                input_raw_data_ids=[raw_id, "artifact_that_does_not_exist"],
                settings={"burst_detection": {"min_photons": 10}},
            )

        assert _count(db, "mmfdb_operation", "operation_id", "proc_missing") == 0
        assert _count(db, "mmfdb_operation_artifact", "operation_id", "proc_missing") == 0
        assert _count(db, "mmfdb_audit_log", "target_id", "proc_missing") == 0


def test_add_processing_run_rolls_back_a_late_failure(tmp_path: Path) -> None:
    """A failure after the operation row is inserted rolls the whole run back.

    The input check runs first, so it alone cannot exercise the transaction.
    Failing the trailing audit-log write instead proves the wrapping savepoint
    really does undo the operation row and its provenance edges.
    """
    raw_path = tmp_path / "input.ptu"
    raw_path.write_bytes(b"fake tttr")

    with MFDatabase(tmp_path / "test.db") as db:
        db.add_sample("sample_1")
        db.add_experiment("exp_1", sample_id="sample_1", status="complete")
        raw_id = _raw_artifact(db, raw_path)

        def _boom(*args, **kwargs):
            raise RuntimeError("audit log unavailable")

        db.add_audit_log = _boom
        with pytest.raises(RuntimeError, match="audit log unavailable"):
            db.add_processing_run(
                run_id="proc_late_failure",
                experiment_id="exp_1",
                input_raw_data_ids=[raw_id],
                settings={"burst_detection": {"min_photons": 10}},
            )
        del db.add_audit_log

        assert _count(db, "mmfdb_operation", "operation_id", "proc_late_failure") == 0
        assert _count(db, "mmfdb_operation_artifact", "operation_id", "proc_late_failure") == 0
        assert _count(db, "mmfdb_audit_log", "target_id", "proc_late_failure") == 0

        # The repository is still usable — the savepoint released cleanly.
        ok_id = db.add_processing_run(
            run_id="proc_ok",
            experiment_id="exp_1",
            input_raw_data_ids=[raw_id],
            settings={"burst_detection": {"min_photons": 10}},
        )
        assert ok_id == "proc_ok"
        assert _count(db, "mmfdb_operation", "operation_id", "proc_ok") == 1
        assert _count(db, "mmfdb_operation_artifact", "operation_id", "proc_ok") == 1


def test_artifact_md5_is_labelled_as_md5(tmp_path: Path) -> None:
    """``add_artifact(md5=…)`` records the algorithm, never the sha256 default."""
    digest = "d41d8cd98f00b204e9800998ecf8427e"

    with MFDatabase(tmp_path / "test.db") as db:
        artifact_id = db.add_artifact(
            artifact_id="artifact_md5",
            artifact_kind="raw_data",
            name="digest-labelled",
            storage_mode="local_file",
            file_path=str(tmp_path / "input.ptu"),
            md5=digest,
        )
        row = db.conn.execute(
            "SELECT checksum, checksum_algorithm FROM mmfdb_artifact WHERE artifact_id = ?",
            (artifact_id,),
        ).fetchone()

    assert row["checksum"] == digest
    assert row["checksum_algorithm"] == "md5"


def test_artifact_without_a_digest_claims_no_algorithm(tmp_path: Path) -> None:
    """No digest means no algorithm label — not an unbacked sha256 claim."""
    with MFDatabase(tmp_path / "test.db") as db:
        artifact_id = db.add_artifact(
            artifact_id="artifact_no_digest",
            artifact_kind="raw_data",
            name="no-digest",
            storage_mode="local_file",
            file_path=str(tmp_path / "input.ptu"),
        )
        row = db.conn.execute(
            "SELECT checksum, checksum_algorithm FROM mmfdb_artifact WHERE artifact_id = ?",
            (artifact_id,),
        ).fetchone()

    assert row["checksum"] is None
    assert row["checksum_algorithm"] is None
