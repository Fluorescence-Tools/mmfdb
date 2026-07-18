"""Publication exports built from a recorded workflow run."""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

from mmfdb.provenance.result_registry import set_global_db
from mmfdb.repository import MFDatabase
from mmfdb.workflow.export import export_publication, methods_report, write_bundle
from mmfdb.workflow.runner import run_workflow
from mmfdb.workflow.spec import load_workflow_text

_CLI = """
import argparse, json
p = argparse.ArgumentParser()
p.add_argument("inp")
p.add_argument("--out", required=True)
a = p.parse_args()
open(a.out, "w").write("proximity_ratio\\n0.4\\n0.6\\n")
print(json.dumps({"n_bursts": 2}))
"""


def _run(tmp_path):
    (tmp_path / "m.spc").write_bytes(b"\x00\x01\x02")
    tool = tmp_path / "tool.py"
    tool.write_text(_CLI)
    wf_text = f"""
version: 1
name: smfret_demo
description: burst analysis for the paper
sources:
  raw: {{path: m.spc}}
steps:
  - id: bursts
    operation_type: burst_selection
    software: {{package: demo_tool, version: "1.2"}}
    inputs: {{photons: raw}}
    params: {{min_photons: 20}}
    cmd: "{sys.executable} {tool} {{inputs.photons}} --out {{outputs.table}}"
    outputs:
      table: {{kind: burst_table, path: bursts.csv}}
publish: {{seed: bursts.table, formats: [report, mmcif, bundle]}}
"""
    wf = load_workflow_text(wf_text, base_dir=tmp_path)
    db = MFDatabase(str(tmp_path / "db.sqlite"))
    run = run_workflow(wf, db, workdir=tmp_path)
    return wf, db, run


def test_methods_report_names_tool_and_command(tmp_path):
    _wf, db, run = _run(tmp_path)
    try:
        report = methods_report(db, run.seed_artifact_id, title="smfret_demo")
        assert "demo_tool 1.2" in report
        assert "burst_selection" in report
        assert "`min_photons` = 20" in report
        assert "n_bursts = 2" in report
        assert run.seed_artifact_id in report
    finally:
        set_global_db(None)
        db.close()


def test_bundle_is_readable_without_a_database(tmp_path):
    """The default bundle is standards-only: no opaque SQLite snapshot."""
    _wf, db, run = _run(tmp_path)
    try:
        zip_path = write_bundle(db, run.seed_artifact_id, tmp_path / "dep.zip")
        with zipfile.ZipFile(zip_path) as z:
            names = set(z.namelist())
        assert {"README.txt", "provenance_graph.json", "methods.md", "metadata.cif", "manifest.json"} <= names
        assert "database_snapshot.db" not in names  # readable, not a DB blob
    finally:
        set_global_db(None)
        db.close()


def test_archive_bundle_reconstitutes_a_second_instance(tmp_path):
    """With include_snapshot, the bundle still round-trips into a fresh MMFDB."""
    _wf, db, run = _run(tmp_path)
    try:
        zip_path = write_bundle(
            db, run.seed_artifact_id, tmp_path / "archive.zip", include_snapshot=True
        )
        unpacked = tmp_path / "unpacked"
        with zipfile.ZipFile(zip_path) as z:
            assert "database_snapshot.db" in set(z.namelist())
            z.extractall(unpacked)
        with MFDatabase(str(unpacked / "database_snapshot.db")) as second:
            artifacts = {a["artifact_id"] for a in second.list_artifacts()}
        assert run.seed_artifact_id in artifacts
    finally:
        set_global_db(None)
        db.close()


def test_release_bundle_ships_named_members(tmp_path):
    """The release example packages the whole set into a ZIP with real filenames."""
    from mmfdb.repository import MFDatabase
    from mmfdb.workflow import load_workflow, run_workflow, write_bundle

    wf_path = Path(__file__).resolve().parents[1] / "examples" / "workflows" / "07_release.yaml"
    wf = load_workflow(wf_path)
    db = MFDatabase(str(tmp_path / "db.sqlite"))
    try:
        run = run_workflow(wf, db, workdir=tmp_path / "work")
        zip_path = write_bundle(db, run.seed_artifact_id, tmp_path / "release.zip")
        with zipfile.ZipFile(zip_path) as z:
            names = set(z.namelist())
        # every member of the released set is present under its recognizable name
        for member in ("bursts.csv", "bursts_rep1.csv", "bursts_rep2.csv", "manifest.csv"):
            assert f"external_data/{member}" in names, member
        # readable, standards-only: metadata + provenance, no SQLite blob
        assert {"metadata.cif", "provenance_graph.json", "methods.md", "README.txt"} <= names
        assert "database_snapshot.db" not in names
    finally:
        set_global_db(None)
        db.close()


def test_export_publication_writes_all_formats(tmp_path):
    _wf, db, run = _run(tmp_path)
    try:
        produced = export_publication(
            db, run.seed_artifact_id, tmp_path / "out", ["report", "mmcif", "bundle"], name="smfret_demo"
        )
        assert set(produced) == {"report", "mmcif", "bundle"}
        for path in produced.values():
            assert Path(path).is_file()
    finally:
        set_global_db(None)
        db.close()
