"""Tests for PDBx/PDB-IHM/FLR CIF import support."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from mmfdb.samples.importer import import_structure_file
from mmfdb.repository import MFDatabase

#: The standard part of a minimal FLR CIF fixture; the extension categories are
#: appended by :func:`_cif_with_extension_namespace`.
_FLR_PREAMBLE = (
    "data_test\n"
    "_flr_probe_list.probe_id 1\n"
    "_flr_probe_list.chromophore_name Alexa488\n"
    "_flr_probe_list.reactive_probe_flag no\n"
    "_flr_probe_list.reactive_probe_name ?\n"
    "_flr_probe_list.probe_origin extrinsic\n"
    "_flr_probe_list.probe_link_type covalent\n"
    "_flr_sample.id sample_import\n"
    "_flr_sample.entity_assembly_id assembly_import\n"
    "_flr_sample.num_of_probes 1\n"
    "_flr_sample.sample_condition_id condition_import\n"
    "_flr_sample.sample_description Imported\n"
    "_flr_sample.sample_details Details\n"
    "_flr_sample.solvent_phase liquid\n"
    "_flr_sample_condition.id condition_import\n"
    "_flr_sample_condition.details pH=7\n"
    "_flr_sample_probe_details.sample_probe_id 1\n"
    "_flr_sample_probe_details.sample_id sample_import\n"
    "_flr_sample_probe_details.probe_id 1\n"
    "_flr_sample_probe_details.fluorophore_type donor\n"
    "_flr_sample_probe_details.description donor\n"
    "_flr_sample_probe_details.poly_probe_position_id ?\n"
)

_EXTENSION_CATEGORIES = (
    "{ns}_probe_property.probe_id 1\n"
    "{ns}_probe_property.property_name abs_max\n"
    "{ns}_probe_property.property_value 495\n"
    "{ns}_probe_property.unit nm\n"
    "{ns}_probe_property.details seed\n"
    "{ns}_probe_spectrum.probe_id 1\n"
    "{ns}_probe_spectrum.spectrum_type absorption\n"
    '{ns}_probe_spectrum.wavelengths "455 495 535"\n'
    '{ns}_probe_spectrum.intensity_values "0.2 1.0 0.2"\n'
    "{ns}_probe_spectrum.wavelength_unit nm\n"
    "{ns}_probe_spectrum.intensity_unit normalized\n"
    "{ns}_probe_spectrum.details spectrum\n"
)


def _cif_with_extension_namespace(namespace: str) -> str:
    """Build the fixture CIF with its extension categories in *namespace*."""
    return _FLR_PREAMBLE + _EXTENSION_CATEGORIES.format(ns=namespace)


@pytest.mark.parametrize("namespace", ["_mmfdb", "_chisurf"])
def test_import_flr_cif_extension_categories(namespace):
    """Both the canonical and the legacy branded extension namespace import."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "sample.cif"
        path.write_text(_cif_with_extension_namespace(namespace))
        db = MFDatabase(":memory:")
        try:
            summary = import_structure_file(db, path)
            assert summary["samples"]
            sample_id = summary["samples"][0]
            assert db.get_sample(sample_id) is not None
            props = {
                p["property_name"]: p["property_value"]
                for p in db.get_optical_properties(1)
            }
            assert props["abs_max"] == "495"
            assert db.get_spectrum_record(1, "absorption") is not None
        finally:
            db.close()
