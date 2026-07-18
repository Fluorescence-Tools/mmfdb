"""Workflow YAML parsing/validation (no execution, no database)."""

from __future__ import annotations

import pytest

from mmfdb.workflow.spec import WorkflowError, load_workflow_text

_VALID = """
version: 1
name: demo
description: two steps, one chained on the other
sources:
  raw:
    path: data/m.spc
    metadata: {file_type: SPC-130}
steps:
  - id: bursts
    operation_type: burst_selection
    software: {package: tool, version: "1.0"}
    inputs: {photons: raw}
    params: {min_photons: 20}
    cmd: "tool {inputs.photons} --out {outputs.table}"
    outputs:
      table: {kind: burst_table, path: bursts.csv}
  - id: fit
    inputs: {bursts: bursts.table}
    python: pkg.mod:run
    outputs:
      result: {kind: fit_result}
publish: {seed: fit.result, formats: [report, mmcif, bundle]}
"""


def test_valid_workflow_parses_and_orders():
    wf = load_workflow_text(_VALID, base_dir=".")
    assert wf.name == "demo"
    assert set(wf.sources) == {"raw"}
    assert [s.id for s in wf.ordered_steps()] == ["bursts", "fit"]
    assert wf.step("bursts").kind == "cmd"
    assert wf.step("fit").kind == "python"
    assert wf.publish.formats == ("report", "mmcif", "bundle")


def test_unknown_top_level_key_rejected():
    with pytest.raises(WorkflowError, match="unknown keys"):
        load_workflow_text("version: 1\nname: x\nbogus: 1\n")


def test_version_must_be_one():
    with pytest.raises(WorkflowError, match="unsupported workflow version"):
        load_workflow_text("version: 2\nname: x\n")


def test_step_needs_exactly_one_runner():
    doc = """
version: 1
name: x
steps:
  - id: a
    cmd: "tool"
    python: "pkg:run"
    outputs: {o: {}}
"""
    with pytest.raises(WorkflowError, match="exactly one of 'cmd' or 'python'"):
        load_workflow_text(doc)


def test_unknown_input_reference_rejected():
    doc = """
version: 1
name: x
steps:
  - id: a
    cmd: "tool {inputs.p}"
    inputs: {p: missing_source}
    outputs: {o: {}}
"""
    with pytest.raises(WorkflowError, match="unknown source"):
        load_workflow_text(doc)


def test_cycle_detected():
    doc = """
version: 1
name: x
steps:
  - id: a
    cmd: "t {inputs.x}"
    inputs: {x: b.o}
    outputs: {o: {}}
  - id: b
    cmd: "t {inputs.x}"
    inputs: {x: a.o}
    outputs: {o: {}}
"""
    with pytest.raises(WorkflowError, match="cycle"):
        load_workflow_text(doc)


def test_duplicate_step_id_rejected():
    doc = """
version: 1
name: x
steps:
  - id: a
    cmd: "t"
    outputs: {o: {}}
  - id: a
    cmd: "t"
    outputs: {o: {}}
"""
    with pytest.raises(WorkflowError, match="duplicate step id"):
        load_workflow_text(doc)


def test_source_and_step_name_collision_rejected():
    doc = """
version: 1
name: x
sources:
  a: {path: f}
steps:
  - id: a
    cmd: "t"
    outputs: {o: {}}
"""
    with pytest.raises(WorkflowError, match="both source and step"):
        load_workflow_text(doc)
