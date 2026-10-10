"""A ``.pto`` exports to mmCIF python-ihm reads cleanly, and imports back unchanged.

``.pto`` -> graph -> mmCIF -> python-ihm -> graph -> database -> graph: the
operations, artifacts, edges and parameters at the end equal those at the
start, for a project (``*.cs.pto``) and for a measurement with several runs.
The ``ihm.reader.UnknownCategoryWarning`` filter of the test configuration
makes an undeclared category an error here.
"""

from __future__ import annotations

import json
import warnings

import pytest

tttrlib = pytest.importorskip("tttrlib")
pytest.importorskip("ihm")

from mmfdb.project import pto as project_pto
from mmfdb.provenance.pto_graph import (
    canonical,
    graph_from_db,
    graph_from_pto,
    import_graph,
    read_graph_cif,
    write_graph_cif,
)
from mmfdb.repository import MFDatabase


def _describe(handle, uid, **kw):
    tttrlib.pto_describe(handle, uid, software="chisurf 9", dictionary_version="1",
                         dictionary_hash="h", **kw)


@pytest.fixture
def measurement(tmp_path):
    """A photon stream, a burst table, a fused table of two parents, and an MLE run."""
    path = tmp_path / "m000.pto"
    h = tttrlib.PtoFile()
    assert h.create(str(path), "t")
    raw = h.add("tttr_photon_stream", "ptu", "m000.ptu", b"photons" * 20)
    _describe(h, raw, data_format="ptu", checksum="ab" * 32, size_bytes=140, file_path="m000.ptu")

    def table(name, kind, op, params, parents, **kw):
        uid = h.add(kind, "raw", name, b"x" * 16)
        _describe(h, uid, data_format="dstore", row_grain=kw.pop("row_grain", "burst"),
                  operation_type=op, parameters=params, derived_from=parents, **kw)
        return uid

    first = table("bursts", "burst_table", "burst_selection", {"min_photons": 60}, [raw])
    second = table("bursts 90", "burst_table", "burst_selection", {"min_photons": 90}, [raw])
    table("fused", "burst_table", "burst_fusion", {"window": 1.5}, [first, second],
          source_row_column="burst", target_row_column="Number of Photons")
    # one run, two outputs
    table("fit green", "burst_table", "burst_lifetime_fitting", {"irf": "a"}, [first])
    table("fit red", "burst_table", "burst_lifetime_fitting", {"irf": "a"}, [first])
    assert h.commit()
    h.close()
    return path


def _project_payload():
    def parameter(uid, name, value, link=None):
        return {"uid": uid, "name": name, "value": value, "fixed": False, "bounds": [0.0, 10.0],
                "bounds_on": "value", "error_estimate": 0.25, "link_target": link}

    def member(uid, params):
        return {"uid": uid, "dataset_uid": "ds1", "dependencies": {},
                "model": {"model_module": "m", "model_class": "C", "parameters": params}}

    return {
        "project_format_version": 5, "meta": {"name": "round trip"},
        "datasets": {"ds1": {"filename": "decay.dat", "name": "decay", "reader": {}, "experiment": {}}},
        "experiments": {}, "ui": {}, "extra": {}, "dependency_edges": [], "parameters": {},
        "fits": [
            {"uid": "fitA", "name": "A", "kind": "single",
             "members": [member("mA", [parameter("pA", "tau", 4.0)])]},
            {"uid": "fitB", "name": "B", "kind": "single",
             "members": [member("mB", [parameter(
                 "pB", "tau", 4.1, {"fit_uid": "fitA", "member_uid": "mA", "parameter_uid": "pA"})])]},
        ],
    }


@pytest.fixture
def project(tmp_path):
    path = tmp_path / "p.cs.pto"
    path.write_bytes(project_pto.write_bundle(_project_payload(), {"decay.dat": b"1 2 3\n"}))
    return path


@pytest.mark.parametrize("fixture_name", ["measurement", "project"])
def test_pto_round_trips_through_mmcif_and_the_database(fixture_name, request, tmp_path):
    source = request.getfixturevalue(fixture_name)
    graph = graph_from_pto(source)
    assert graph["operations"] and graph["artifacts"] and graph["edges"]

    cif = tmp_path / "out.cif"
    write_graph_cif(graph, cif)
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # no UnknownCategoryWarning, no unknown keyword
        assert read_graph_cif(cif) == graph

    with MFDatabase(str(tmp_path / "import.sqlite")) as db:
        import_graph(db, read_graph_cif(cif))
        imported = graph_from_db(db)
    assert imported == graph


def test_the_measurement_graph_says_what_each_run_was(measurement):
    graph = graph_from_pto(measurement)
    kinds = sorted(op["operation_type"] for op in graph["operations"].values())
    assert kinds == ["burst_fusion", "burst_lifetime_fitting", "burst_selection", "burst_selection"]
    fusion = next(o for o in graph["operations"].values() if o["operation_type"] == "burst_fusion")
    assert fusion["settings"] == {"window": 1.5}
    joins = [e for e in graph["edges"] if e["source_row_column"]]
    assert [(e["source_row_column"], e["target_row_column"]) for e in joins] == [
        ("burst", "Number of Photons")] * 2
    # both outputs of the one fit run hang off one operation
    fit = [e for e in graph["edges"] if e["relationship_type"] == "produced"
           and graph["operations"][e["source_node_id"]]["operation_type"] == "burst_lifetime_fitting"]
    assert len(fit) == 2 and len({e["source_node_id"] for e in fit}) == 1


def test_the_project_graph_carries_parameters_and_their_link(project):
    graph = graph_from_pto(project)
    assert sorted(p["value"] for p in graph["parameters"]) == [4.0, 4.1]
    assert any(e["relationship_type"] == "parameter_depends_on" for e in graph["edges"])
    assert json.dumps(canonical(graph)) == json.dumps(graph)


def test_the_command_line_exports_and_imports(measurement, tmp_path):
    from click.testing import CliRunner

    from mmfdb.cli import cli

    cif = tmp_path / "m.cif"
    runner = CliRunner()
    result = runner.invoke(cli, ["project", "export-cif", str(measurement), "--output", str(cif)])
    assert result.exit_code == 0, result.output
    db_path = tmp_path / "cli.sqlite"
    result = runner.invoke(cli, ["project", "import-cif", str(cif), "--database", str(db_path)])
    assert result.exit_code == 0, result.output
    with MFDatabase(str(db_path)) as db:
        assert graph_from_db(db) == graph_from_pto(measurement)


def test_the_structure_importer_reads_the_graph_without_a_warning(measurement, tmp_path):
    from mmfdb.samples.importer import import_structure_file

    cif = tmp_path / "m.cif"
    write_graph_cif(graph_from_pto(measurement), cif)
    with MFDatabase(str(tmp_path / "imp.sqlite")) as db:
        summary = import_structure_file(db, cif)
        assert not [w for w in summary["warnings"] if "parse failed" in w], summary["warnings"]
        assert summary["provenance"]["operations"] == 4


def test_every_derived_artifact_reaches_the_primary_data(measurement, project, tmp_path):
    """Lineage from any derived table ends at the photons, the image or the vendor file."""
    from mmfdb.provenance.pto_graph import untraceable

    assert untraceable(graph_from_pto(measurement)) == []
    assert untraceable(graph_from_pto(project)) == []
    # and the same walk holds after the trip through mmCIF and the database
    cif = tmp_path / "m.cif"
    write_graph_cif(graph_from_pto(measurement), cif)
    with MFDatabase(str(tmp_path / "t.sqlite")) as db:
        import_graph(db, read_graph_cif(cif))
        assert untraceable(graph_from_db(db)) == []


def test_a_result_without_a_parent_is_reported(measurement):
    from mmfdb.provenance.pto_graph import untraceable

    graph = graph_from_pto(measurement)
    parents = {e["target_node_id"] for e in graph["edges"] if e["relationship_type"] == "derived_from"}
    orphan = next(a for a, row in graph["artifacts"].items()
                  if row["artifact_kind"] == "burst_table" and a not in parents)
    cut = {**graph, "edges": [e for e in graph["edges"] if e["source_node_id"] != orphan
                              and e["target_node_id"] != orphan]}
    assert untraceable(cut) == [orphan]
