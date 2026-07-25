"""Tests for diffusion coefficients stored as probe (dye) properties."""

from __future__ import annotations

from mmfdb.models import (
    DIFFUSION_PROPERTY_NAME,
    DIFFUSION_PROPERTY_UNIT,
    REFERENCE_DIFFUSION,
)
from mmfdb.repository import MFDatabase


def _open(tmp_path):
    return MFDatabase(str(tmp_path / "reference_diffusion.db"))


def test_reference_table_is_shipped():
    names = {entry["name"] for entry in REFERENCE_DIFFUSION}
    assert {"Rhodamine 6G", "Rhodamine 110", "Cy5"} <= names
    for entry in REFERENCE_DIFFUSION:
        assert entry["d25_um2_s"] > 0.0
        assert entry["sources"], f"{entry['name']} carries no citation"


def test_import_stores_diffusion_as_a_probe_property(tmp_path):
    with _open(tmp_path) as db:
        counts = db.import_reference_diffusion()
        assert counts["diffusion_properties"] == len(REFERENCE_DIFFUSION)

    with _open(tmp_path) as db:
        row = db.conn.execute(
            "SELECT o.property_value, o.unit FROM optical_properties o "
            "JOIN probes p ON p.probe_id = o.probe_id "
            "WHERE p.chromophore_name = 'Rhodamine 6G' AND o.property_name = ?",
            (DIFFUSION_PROPERTY_NAME,),
        ).fetchone()
        assert row is not None
        assert float(row["property_value"]) == 414.0
        assert row["unit"] == DIFFUSION_PROPERTY_UNIT


def test_import_is_idempotent_and_reuses_existing_probes(tmp_path):
    with _open(tmp_path) as db:
        first = db.import_reference_diffusion()
        second = db.import_reference_diffusion()
        assert first["probes"] == len(REFERENCE_DIFFUSION)
        assert second["probes"] == 0
        assert second["matched"] == len(REFERENCE_DIFFUSION)
        total = db.conn.execute(
            "SELECT COUNT(*) FROM optical_properties WHERE property_name = ?",
            (DIFFUSION_PROPERTY_NAME,),
        ).fetchone()[0]
        assert total == len(REFERENCE_DIFFUSION)


def test_import_attaches_to_an_existing_probe_by_alias(tmp_path):
    with _open(tmp_path) as db:
        probe_id = db.find_or_add_probe("Rh6G", category="organic_dye")
        counts = db.import_reference_diffusion()
        assert counts["matched"] >= 1
        row = db.conn.execute(
            "SELECT property_value FROM optical_properties "
            "WHERE probe_id = ? AND property_name = ?",
            (probe_id, DIFFUSION_PROPERTY_NAME),
        ).fetchone()
        assert row is not None and float(row["property_value"]) == 414.0
        # No second "Rhodamine 6G" probe was created for the same species.
        assert db.conn.execute(
            "SELECT COUNT(*) FROM probes WHERE chromophore_name = 'Rhodamine 6G'"
        ).fetchone()[0] == 0


def test_get_diffusion_reference_seeds_and_returns_sources(tmp_path):
    with _open(tmp_path) as db:
        species = db.get_diffusion_reference()
        assert len(species) == len(REFERENCE_DIFFUSION)
        by_name = {entry["name"]: entry for entry in species}
        rh6g = by_name["Rhodamine 6G"]
        assert rh6g["d25_um2_s"] == 414.0
        assert rh6g["unit"] == DIFFUSION_PROPERTY_UNIT
        assert rh6g["sources"][0]["citation"].startswith("Kapusta")
        assert "2fFCS" in rh6g["sources"][0]["methods"]


def test_get_diffusion_reference_can_stay_read_only(tmp_path):
    with _open(tmp_path) as db:
        assert db.get_diffusion_reference(seed_if_empty=False) == []
        assert db.conn.execute(
            "SELECT COUNT(*) FROM optical_properties WHERE property_name = ?",
            (DIFFUSION_PROPERTY_NAME,),
        ).fetchone()[0] == 0
