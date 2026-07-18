"""Declarative, YAML-defined analysis workflows for MMFDB.

Define a pipeline in a plain YAML file — its raw *sources*, a sequence of
*steps* that each run a command-line tool or a Python callable, and what to
*publish* — and MMFDB coordinates the run, mapping every step onto its
provenance graph (``source → tool → result``) and exporting the workflow and its
results for publication.

See :mod:`mmfdb.workflow.spec` for the schema, :mod:`mmfdb.workflow.runner` for
execution, and :mod:`mmfdb.workflow.export` for the publication outputs.
"""

from __future__ import annotations

from mmfdb.workflow.export import (
    export_publication,
    methods_report,
    read_single_cif,
    workflow_dictionary_text,
    write_bundle,
    write_mmcif,
    write_single_cif,
)
from mmfdb.workflow.runner import StepRun, WorkflowRun, WorkflowRunError, run_workflow
from mmfdb.workflow.spec import (
    OutputSpec,
    PublishSpec,
    SourceSpec,
    StepSpec,
    Workflow,
    WorkflowError,
    load_workflow,
    load_workflow_text,
)

__all__ = [
    "Workflow",
    "WorkflowError",
    "SourceSpec",
    "StepSpec",
    "OutputSpec",
    "PublishSpec",
    "load_workflow",
    "load_workflow_text",
    "run_workflow",
    "WorkflowRun",
    "StepRun",
    "WorkflowRunError",
    "methods_report",
    "write_mmcif",
    "write_bundle",
    "write_single_cif",
    "read_single_cif",
    "workflow_dictionary_text",
    "export_publication",
]
