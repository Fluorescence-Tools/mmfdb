"""The standard FLR export writes dictionary items, never database column names.

python-ihm reports an unknown keyword for ``_flr_fret_analysis.analysis_id`` --
the SQL column -- where flrCIF defines ``_flr_fret_analysis.id``. Every item of
every non-extension loop must be declared by a bundled dictionary, and a read
with unknown-keyword warnings on must leave nothing the dictionaries do not
declare.
"""

from __future__ import annotations

import re
import warnings

import ihm.reader
import numpy as np
import pytest

from mmfdb import cif_writer
from mmfdb.provenance.pto_graph import _declared_item_checker
from mmfdb.repository import MFDatabase


@pytest.fixture
def populated():
    """A database with a row for every standard category the export writes."""
    db = MFDatabase(":memory:", enforce_foreign_keys=False)
    tid = db.add_probe_type("organic_dye", "Organic dye")
    donor = db.add_probe("Alexa488", tid, category="organic_dye", description="donor dye")
    acceptor = db.add_probe("Alexa594", tid, category="organic_dye")
    db.conn.execute(
        "UPDATE probes SET chromophore_chem_descriptor_id = 'desc1', "
        "chromophore_center_atom = 'C1' WHERE probe_id = ?", (donor,))
    db.add_sample("s1", uuid="u1", num_of_probes=2, solvent_phase="liquid")
    sp1 = db.add_sample_probe("s1", donor, fluorophore_type="donor")
    sp2 = db.add_sample_probe("s1", acceptor, fluorophore_type="acceptor")
    db.add_poly_probe_position(donor, 1, 10, residue_name="CYS", description="site A")
    db.add_fret_forster_radius("fr1", "s1", donor, acceptor, 52.0,
                               kappa_squared=0.67, refractive_index=1.33, details="calc")
    db.update_analysis_record("an1", sample_id="s1", type="intensity-based",
                              method="PDA", sample_probe_id_1=sp1, sample_probe_id_2=sp2,
                              forster_radius_id="fr1")
    db.conn.execute(
        "INSERT INTO struct_ref (ref_id, entity_id, db_name, db_code, pdbx_db_accession, "
        "pdbx_seq_one_letter_code, organism) VALUES ('1', '1', 'UNP', 'X_HUMAN', 'P1', 'MAA', 'H')")
    db.conn.execute(
        "INSERT INTO struct_ref_seq (align_id, ref_id, seq_align_beg, seq_align_end, "
        "db_align_beg, db_align_end, pdbx_db_accession) VALUES ('1', '1', 1, 3, 1, 3, 'P1')")
    db.conn.execute(
        "INSERT INTO struct_ref_seq_dif (id, align_id, seq_num, mon_id, db_mon_id, "
        "pdbx_ordinal) VALUES (1, '1', 2, 'ALA', 'GLY', 1)")
    db.add_external_file("/tmp/x.ptu", file_format="ptu", md5="0" * 32)
    db.add_analysis_data("an1", "decay", np.array([0.0, 1.0]), np.array([1.0, 2.0]),
                         data_name="d", x_unit="ns", y_unit="counts")
    return db


def test_every_standard_item_is_declared_by_a_dictionary(populated, monkeypatch):
    written: dict[str, list[str]] = {}
    original = cif_writer._Loop.__init__

    def record(self, output, category, columns):
        written[category.rstrip(".")] = list(columns)
        original(self, output, category, columns)

    monkeypatch.setattr(cif_writer._Loop, "__init__", record)
    populated.export_flr_cif_to_text(analysis_id="an1")

    declared = _declared_item_checker()
    # python-ihm reads these keys; the dictionary parser misses them (it records
    # the key in the category context, not as an item).
    keys = {"_struct_ref.id", "_struct_ref_seq.align_id", "_struct_ref_seq_dif.align_id"}
    undeclared = [
        f"{category}.{item}"
        for category, items in written.items()
        if not category.startswith("_mmfdb_")
        for item in items
        if not declared(f"{category}.{item}") and f"{category}.{item}" not in keys
    ]
    assert not undeclared


def test_a_read_with_keyword_warnings_on_finds_no_undeclared_keyword(populated, tmp_path):
    path = tmp_path / "export.cif"
    path.write_text(populated.export_flr_cif_to_text(analysis_id="an1"), encoding="utf-8")
    declared = _declared_item_checker()
    with warnings.catch_warnings(record=True) as caught, open(path, encoding="utf-8") as handle:
        warnings.simplefilter("always")
        ihm.reader.read(handle, warn_unknown_keyword=True)
    unknown = [
        m.group(1)
        for w in caught
        if issubclass(w.category, ihm.reader.UnknownKeywordWarning)
        and (m := re.search(r"Unknown keyword (\S+) encountered", str(w.message)))
        and not declared(m.group(1))
    ]
    assert not unknown


def test_the_analysis_row_carries_its_flrcif_identity(populated):
    text = populated.export_flr_cif_to_text(analysis_id="an1")
    assert "_flr_fret_analysis.id" in text
    assert "_flr_fret_analysis.analysis_id" not in text
    assert "_flr_fret_analysis.type" in text
    assert "_flr_probe_descriptor.chromophore_chem_descriptor_id" in text
    assert "_flr_probe_list.chromophore_chem_descriptor_id" not in text
    assert "_flr_poly_probe_position.seq_id" in text
    assert "_flr_poly_probe_position.residue_number" not in text
    assert "_struct_ref.id" in text and "_struct_ref.ref_id" not in text
