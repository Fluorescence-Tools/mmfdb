"""Tests for seeding ChiSurf's built-in default fluorophore spectra."""

from __future__ import annotations

import numpy as np

from mmfdb.models import DEFAULT_FLUOROPHORE_SPECTRA
from mmfdb.repository import MFDatabase


def _open(tmp_path):
    return MFDatabase(str(tmp_path / "default_spectra.db"))


def test_import_default_spectra_seeds_named_probes(tmp_path):
    with _open(tmp_path) as db:
        counts = db.import_default_spectra()
        assert counts["probes"] == len(DEFAULT_FLUOROPHORE_SPECTRA)

    # Reopen: the seed must be durably committed, including maxima-only probes
    # (e.g. Trp) that carry no spectrum curves.
    with _open(tmp_path) as db:
        for name in DEFAULT_FLUOROPHORE_SPECTRA:
            row = db.conn.execute(
                "SELECT verification_status, quality, source FROM probes "
                "WHERE chromophore_name = ? AND source = 'chisurf_default'",
                (name,),
            ).fetchone()
            assert row is not None, f"{name} was not durably seeded"
            assert row["verification_status"] == "approved"
            assert row["quality"] == "high"


def test_import_default_spectra_writes_decodable_curves(tmp_path):
    with _open(tmp_path) as db:
        db.import_default_spectra()
        probe = db.conn.execute(
            "SELECT probe_id FROM probes WHERE chromophore_name = 'Cy3B'"
        ).fetchone()
        spectra = db.conn.execute(
            "SELECT spectrum_type, wavelengths, intensity_values FROM spectra "
            "WHERE probe_id = ? AND deleted_at IS NULL",
            (probe["probe_id"],),
        ).fetchall()
        types = {s["spectrum_type"] for s in spectra}
        assert {"absorption", "emission"} <= types
        for spec in spectra:
            wl = np.frombuffer(spec["wavelengths"], dtype=np.float64)
            iv = np.frombuffer(spec["intensity_values"], dtype=np.float64)
            assert len(wl) == len(iv) > 0


def test_import_default_spectra_is_idempotent(tmp_path):
    with _open(tmp_path) as db:
        first = db.import_default_spectra()
        second = db.import_default_spectra()
        assert first["probes"] == len(DEFAULT_FLUOROPHORE_SPECTRA)
        assert second["probes"] == 0  # reused, no duplicate rows
        total = db.conn.execute(
            "SELECT COUNT(*) FROM probes WHERE source = 'chisurf_default'"
        ).fetchone()[0]
        assert total == len(DEFAULT_FLUOROPHORE_SPECTRA)
