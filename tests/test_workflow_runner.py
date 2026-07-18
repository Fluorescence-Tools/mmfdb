"""Execute a workflow and check it lands correctly in the provenance graph."""

from __future__ import annotations

import sys

import pytest

from mmfdb.provenance.result_registry import set_global_db
from mmfdb.repository import MFDatabase
from mmfdb.workflow.runner import WorkflowRunError, run_workflow
from mmfdb.workflow.spec import load_workflow_text

# A vanilla CLI tool that knows nothing about MMFDB: reads a file, writes a CSV,
# and prints a JSON summary to stdout (mirrors examples/external_tools).
_CLI = """
import argparse, json
p = argparse.ArgumentParser()
p.add_argument("inp")
p.add_argument("--out", required=True)
p.add_argument("--min", type=int, default=0)
a = p.parse_args()
n = len(open(a.inp, "rb").read())
open(a.out, "w").write("proximity_ratio\\n0.4\\n0.6\\n")
print(json.dumps({"n_bursts": 2, "min_photons": a.min, "input_bytes": n}))
"""

# A Python adapter step: writes its output file and returns a summary dict.
_PY = """
def run(ctx):
    ctx.outputs["result"].write_text("E\\n0.5\\n")
    return {"n": 1, "F": ctx.params.get("F")}
"""


def _write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text)
    return path


def _db(tmp_path):
    return MFDatabase(str(tmp_path / "db.sqlite"))


def test_run_workflow_records_provenance(tmp_path, monkeypatch):
    (tmp_path / "m.spc").write_bytes(b"\x00\x01\x02\x03")
    tool = _write(tmp_path, "tool.py", _CLI)
    _write(tmp_path, "wf_helper.py", _PY)
    monkeypatch.syspath_prepend(str(tmp_path))

    wf_text = f"""
version: 1
name: demo
sources:
  raw: {{path: m.spc, metadata: {{file_type: SPC-130}}}}
steps:
  - id: bursts
    operation_type: burst_selection
    software: {{package: demo_tool, version: "1.0"}}
    inputs: {{photons: raw}}
    params: {{min_photons: 5}}
    cmd: "{sys.executable} {tool} {{inputs.photons}} --out {{outputs.table}} --min {{params.min_photons}}"
    outputs:
      table: {{kind: burst_table, path: bursts.csv}}
  - id: fit
    operation_type: analysis
    software: {{package: pyadapter, version: "0.1"}}
    inputs: {{bursts: bursts.table}}
    params: {{F: 6}}
    python: "wf_helper:run"
    outputs:
      result: {{kind: fit_result, path: fit.csv}}
publish: {{seed: fit.result, formats: [report]}}
"""
    wf = load_workflow_text(wf_text, base_dir=tmp_path)
    db = _db(tmp_path)
    try:
        run = run_workflow(wf, db, workdir=tmp_path)

        # sources and step outputs were registered as artifacts
        assert run.sources["raw"]
        table_id = run.steps[0].outputs["table"]
        fit_id = run.steps[1].outputs["result"]
        assert table_id and fit_id
        assert run.seed_artifact_id == fit_id

        # the CLI actually ran (JSON summary captured) and wrote the CSV
        assert (tmp_path / "bursts.csv").read_text().startswith("proximity_ratio")
        assert run.steps[0].invocation.summary["n_bursts"] == 2

        # both operations recorded with their tool + version
        rows = dict(
            db.conn.execute(
                "SELECT software_package, software_version FROM mmfdb_operation "
                "WHERE software_package IN ('demo_tool','pyadapter')"
            ).fetchall()
        )
        assert rows == {"demo_tool": "1.0", "pyadapter": "0.1"}

        # the command line was captured in the operation settings
        settings = db.conn.execute(
            "SELECT settings_json FROM mmfdb_operation WHERE software_package='demo_tool'"
        ).fetchone()[0]
        assert "--min 5" in settings

        # lineage: fit result is downstream of the raw measurement
        graph = db.export_provenance_graph("artifact", run.sources["raw"])
        node_ids = {n["node_id"] for n in graph["nodes"]}
        assert {run.sources["raw"], table_id, fit_id} <= node_ids
    finally:
        set_global_db(None)
        db.close()


def test_missing_source_file_raises(tmp_path):
    wf_text = """
version: 1
name: demo
sources:
  raw: {path: nope.spc}
steps:
  - id: s
    cmd: "true {inputs.raw}"
    inputs: {raw: raw}
    outputs: {o: {kind: processed_data, path: o.txt}}
"""
    wf = load_workflow_text(wf_text, base_dir=tmp_path)
    db = _db(tmp_path)
    try:
        with pytest.raises(WorkflowRunError, match="file not found"):
            run_workflow(wf, db, workdir=tmp_path)
    finally:
        set_global_db(None)
        db.close()


def test_step_that_produces_no_output_raises(tmp_path):
    (tmp_path / "m.spc").write_bytes(b"\x00")
    wf_text = f"""
version: 1
name: demo
sources:
  raw: {{path: m.spc}}
steps:
  - id: s
    cmd: "{sys.executable} -c pass"
    inputs: {{raw: raw}}
    outputs: {{o: {{kind: processed_data, path: missing.txt}}}}
"""
    wf = load_workflow_text(wf_text, base_dir=tmp_path)
    db = _db(tmp_path)
    try:
        with pytest.raises(WorkflowRunError, match="did not produce declared output"):
            run_workflow(wf, db, workdir=tmp_path)
    finally:
        set_global_db(None)
        db.close()
