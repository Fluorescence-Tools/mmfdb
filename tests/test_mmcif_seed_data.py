"""Tests for curated sample database seed data."""

from __future__ import annotations

import tempfile
from pathlib import Path

from mmfdb.repository import MFDatabase
from mmfdb.samples.seed_data import seed_curated_database


def test_seed_curated_database_contains_samples():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "sample_management.db"
        seed_curated_database(db_path)
        with MFDatabase(db_path) as db:
            assert db.get_schema_version() >= 11
            assert len(db.list_samples()) >= 3
            assert len(db.get_probes()) >= 7
            assert len(db.get_sample_probe_mappings()) >= 3
            assert len(db.get_users()) >= 2
            assert len(db.get_devices()) >= 2
            assert len(db.get_experiment_types()) >= 7
            assert len(db.get_experiments()) >= 4
            assert len(db.get_experiment_data("1RTD_spectra_001")) == 1
            assert db.get_experiment_data("1RTD_spectra_001")[0]["reading_options_json"]
            assert len(db.get_sample_key_values("148L_65_141_A488_A594")) >= 2
            assert db.conn.execute("PRAGMA foreign_key_check").fetchall() == []
            assert db.conn.execute(
                "SELECT COUNT(*) FROM flr_fret_forster_radius WHERE sample_id IS NULL"
            ).fetchone()[0] == 0


def test_seed_curated_database_is_idempotent(tmp_path):
    db_path = tmp_path / "sample_management.db"
    seed_curated_database(db_path)
    with MFDatabase(db_path) as db:
        before = {
            table: db.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "flr_sample",
                "flr_experiment",
                "flr_sample_probe",
                "flr_fret_forster_radius",
            )
        }

    seed_curated_database(db_path)
    with MFDatabase(db_path) as db:
        after = {
            table: db.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in before
        }
        assert after == before
        assert db.conn.execute("PRAGMA foreign_key_check").fetchall() == []
