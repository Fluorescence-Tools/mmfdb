"""The single self-contained deposit CIF: valid, self-describing, reproducible."""

from __future__ import annotations

from pathlib import Path

from mmfdb.provenance.result_registry import set_global_db
from mmfdb.repository import MFDatabase
from mmfdb.workflow import (
    load_workflow,
    read_single_cif,
    run_workflow,
    workflow_dictionary_text,
    write_single_cif,
)

WF_DIR = Path(__file__).resolve().parents[1] / "examples" / "workflows"


def _run(tmp_path, name="04_mixed_cli_python.yaml"):
    wf_path = WF_DIR / name
    wf = load_workflow(wf_path)
    db = MFDatabase(str(tmp_path / "db.sqlite"))
    run = run_workflow(wf, db, workdir=tmp_path / "work")
    return wf, wf_path, db, run


def test_dictionary_defines_the_categories():
    dic = workflow_dictionary_text()
    for category in (
        "save_mmfdb_workflow",
        "save_mmfdb_provenance_operation",
        "save_mmfdb_provenance_edge",
        "save_mmfdb_bundle_file",
        "save_mmfdb_bundle_document",
    ):
        assert category in dic
    # deliberately NOT database-backed
    assert "_mmfdb_schema.table_name" not in dic


def test_single_cif_is_standards_parseable_and_self_describing(tmp_path):
    import pytest

    ihm_reader = pytest.importorskip("ihm.reader")
    wf, wf_path, db, run = _run(tmp_path)
    try:
        cif = write_single_cif(
            db, run.seed_artifact_id, tmp_path / "d.cif",
            workflow=wf, workflow_yaml=wf_path.read_text(),
        )
        text = Path(cif).read_text()
        # references its own dictionary and defines the provenance/workflow blocks
        assert "_audit_conform.dict_name" in text
        assert "mmfdb_workflow_ext.dic" in text
        assert "_mmfdb_provenance_operation.command_line" in text
        assert "_mmfdb_workflow_step.step_id" in text
        # a real mmCIF reader accepts the syntax (base64 ;-blocks and all)
        with open(cif) as handle:
            ihm_reader.read(handle)
    finally:
        set_global_db(None)
        db.close()


def test_single_cif_round_trips_data_and_workflow(tmp_path):
    """Reproducibility: the deposit alone yields the inputs, the workflow, and the dict."""
    wf, wf_path, db, run = _run(tmp_path)
    try:
        cif = write_single_cif(
            db, run.seed_artifact_id, tmp_path / "d.cif",
            workflow=wf, workflow_yaml=wf_path.read_text(),
        )
        extracted = read_single_cif(cif)

        original = (WF_DIR / "data" / "bursts.csv").read_bytes()
        assert extracted["files"]["bursts.csv"] == original       # data recovered byte-for-byte
        assert extracted["files"]["workflow.yaml"].decode() == wf_path.read_text()
        assert "mmfdb_workflow_ext.dic" in extracted["files"]      # self-describing
        assert "methods.md" in extracted["documents"]
    finally:
        set_global_db(None)
        db.close()


def test_cif_publish_format(tmp_path):
    from mmfdb.workflow import export_publication

    wf, wf_path, db, run = _run(tmp_path)
    try:
        produced = export_publication(
            db, run.seed_artifact_id, tmp_path / "pub", ["cif"], name="demo",
            workflow=wf, workflow_yaml=wf_path.read_text(),
        )
        assert set(produced) == {"cif"}
        assert Path(produced["cif"]).name == "demo.deposit.cif"
        assert Path(produced["cif"]).is_file()
    finally:
        set_global_db(None)
        db.close()
