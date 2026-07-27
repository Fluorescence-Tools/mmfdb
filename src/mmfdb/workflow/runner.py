"""Coordinate a workflow: register sources, run each step, record provenance.

The runner is the "MMFDB coordinates, host runs" seam. It executes each step
with a host-neutral executor (a subprocess or a Python callable) and maps the
run onto the existing provenance model — every step becomes one
``mmfdb_operation`` node linking its input artifacts to the output artifacts it
produced, with the tool, version, command line, and parameters recorded so the
lineage ``source → tool → result`` is queryable and exportable afterward.

It reuses :func:`mmfdb.provenance.result_registry.register_result` for object
storage, ACLs, and lifecycle, then stamps the produced operation with the tool
metadata and any additional input edges.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from pathlib import Path

from mmfdb.provenance.result_registry import (
    database_context,
    register_raw_measurement,
    register_result,
)
from mmfdb.security.base import MMFDBClientBase
from mmfdb.workflow.executors import Invocation, StepContext, default_executors
from mmfdb.workflow.spec import StepSpec, Workflow, parse_ref


class WorkflowRunError(RuntimeError):
    """A workflow failed while registering a source or running a step."""


@dataclass
class StepRun:
    """The recorded result of one executed step."""

    step_id: str
    operation_type: str
    outputs: dict[str, str]  # output name -> artifact_id
    invocation: Invocation


@dataclass
class WorkflowRun:
    """The full record of one workflow execution."""

    workflow: str
    sources: dict[str, str]  # source name -> artifact_id
    steps: list[StepRun] = field(default_factory=list)
    seed_artifact_id: str | None = None
    workdir: Path | None = None

    def artifact_for(self, ref: str) -> str | None:
        """Resolve a ``source`` or ``step.output`` reference to an artifact id."""
        base, out_name = parse_ref(ref)
        if out_name is None:
            return self.sources.get(base)
        for step in self.steps:
            if step.step_id == base:
                return step.outputs.get(out_name)
        return None


def run_workflow(
    workflow: Workflow,
    db: MMFDBClientBase,
    *,
    workdir: str | Path | None = None,
    executors: dict | None = None,
) -> WorkflowRun:
    """Execute ``workflow`` against ``db`` and return the recorded run.

    Parameters
    ----------
    workflow:
        A validated :class:`~mmfdb.workflow.spec.Workflow`.
    db:
        An MMFDB client (e.g. :class:`mmfdb.repository.MFDatabase`). Its
        connection is bound for the duration of the run.
    workdir:
        Directory where step output files are written. Defaults to the workflow
        file's ``base_dir``. Relative source paths resolve against ``base_dir``.
    executors:
        Executor registry keyed by step kind; defaults to
        :func:`mmfdb.workflow.executors.default_executors`.
    """
    executors = executors or default_executors()
    work = Path(workdir) if workdir is not None else workflow.base_dir
    work.mkdir(parents=True, exist_ok=True)

    run = WorkflowRun(workflow=workflow.name, sources={}, workdir=work)
    ref_paths: dict[str, Path] = {}

    with database_context(db):
        _register_sources(workflow, db, run, ref_paths)
        for step in workflow.ordered_steps():
            _run_step(workflow, step, db, run, ref_paths, executors, work)

        if workflow.publish is not None:
            run.seed_artifact_id = run.artifact_for(workflow.publish.seed)
    return run


def _register_sources(
    workflow: Workflow,
    db: MMFDBClientBase,
    run: WorkflowRun,
    ref_paths: dict[str, Path],
) -> None:
    for name, source in workflow.sources.items():
        path = _resolve(workflow.base_dir, source.path)
        if not path.is_file():
            raise WorkflowRunError(f"source {name!r} file not found: {path}")
        if source.kind == "raw_measurement":
            artifact_id = register_raw_measurement(str(path), metadata=source.metadata or None, db=db)
        else:
            artifact_id = register_result(
                kind=source.kind, data=str(path), metadata=source.metadata or None, db=db
            )
        if not artifact_id:
            raise WorkflowRunError(f"could not register source {name!r}")
        _neutralize_import_software(db, artifact_id)
        run.sources[name] = artifact_id
        ref_paths[name] = path


def _run_step(
    workflow: Workflow,
    step: StepSpec,
    db: MMFDBClientBase,
    run: WorkflowRun,
    ref_paths: dict[str, Path],
    executors: dict,
    workdir: Path,
) -> None:
    executor = executors.get(step.kind)
    if executor is None:
        raise WorkflowRunError(f"no executor for step {step.id!r} of kind {step.kind!r}")

    input_paths: dict[str, Path] = {}
    input_artifacts: list[str] = []
    for local_name, ref in step.inputs.items():
        if ref not in ref_paths:
            raise WorkflowRunError(f"step {step.id!r} input {local_name!r} unresolved ref {ref!r}")
        input_paths[local_name] = ref_paths[ref]
        artifact_id = run.artifact_for(ref)
        if artifact_id:
            input_artifacts.append(artifact_id)

    output_paths: dict[str, Path] = {}
    for out_name, out_spec in step.outputs.items():
        rel = out_spec.path or f"{step.id}.{out_name}"
        output_paths[out_name] = (workdir / rel).resolve()

    ctx = StepContext(
        step_id=step.id,
        inputs=input_paths,
        outputs=output_paths,
        params=dict(step.params),
        workdir=workdir,
        workflow_dir=workflow.base_dir,
    )

    started = _now()
    invocation: Invocation = executor.execute(step, ctx)
    ended = _now()

    step_run = StepRun(
        step_id=step.id, operation_type=step.operation_type, outputs={}, invocation=invocation
    )
    primary_input = input_artifacts[0] if input_artifacts else ""
    settings = _settings(step, invocation, started, ended)

    for out_name, out_spec in step.outputs.items():
        out_path = output_paths[out_name]
        if not out_path.is_file():
            raise WorkflowRunError(
                f"step {step.id!r} did not produce declared output {out_name!r} at {out_path}"
            )
        artifact_id = register_result(
            kind=out_spec.kind,
            data=str(out_path),
            parent_artifact_id=primary_input,
            operation_type=step.operation_type,
            metadata={"workflow": workflow.name, "step": step.id, "output": out_name},
            db=db,
        )
        if not artifact_id:
            raise WorkflowRunError(f"step {step.id!r} could not register output {out_name!r}")
        op_id = _producing_operation(db, artifact_id)
        if op_id:
            _stamp_operation(db, op_id, step, invocation, settings, started, ended)
            _link_extra_inputs(db, op_id, artifact_id, input_artifacts[1:])
        step_run.outputs[out_name] = artifact_id
        ref_paths[f"{step.id}.{out_name}"] = out_path

    run.steps.append(step_run)


def _settings(step: StepSpec, invocation: Invocation, started: str, ended: str) -> dict:
    """Build the operation settings blob.

    The invocation string and the exit status are *not* included: they are
    first-class ``mmfdb_operation`` columns (see :func:`_stamp_operation`), so
    keeping a second copy here would make them queryable in two disagreeing
    places.
    """
    settings = {
        "step_kind": invocation.kind,
        "params": dict(step.params),
        "started_at": started,
        "ended_at": ended,
    }
    if invocation.summary:
        settings["tool_summary"] = invocation.summary
    if invocation.stdout:
        settings["stdout"] = invocation.stdout
    return settings


def _stamp_operation(
    db: MMFDBClientBase,
    op_id: str,
    step: StepSpec,
    invocation: Invocation,
    settings: dict,
    started: str,
    ended: str,
) -> None:
    """Attach tool/version/command-line to the operation register_result created."""
    db.record_operation(
        operation_id=op_id,
        operation_type=step.operation_type,
        status="succeeded",
        software_package=step.software.package or None,
        software_version=step.software.version or None,
        settings=settings,
        command_line=invocation.command_line,
        exit_code=invocation.returncode,
        started_at=started,
        ended_at=ended,
        metadata={"step": step.id},
    )


def _link_extra_inputs(
    db: MMFDBClientBase, op_id: str, output_artifact_id: str, extra_inputs: list[str]
) -> None:
    """Record inputs beyond the primary one as ports and derived_from edges."""
    for source_id in extra_inputs:
        db.record_operation_link(
            operation_id=op_id, artifact_id=source_id, direction="input", role="source"
        )
        db.add_edge(
            source_node_type="artifact",
            source_node_id=output_artifact_id,
            target_node_type="artifact",
            target_node_id=source_id,
            relationship_type="derived_from",
        )


def _neutralize_import_software(db: MMFDBClientBase, artifact_id: str) -> None:
    """Attribute a source's import operation to MMFDB, not the default host tool.

    ``register_result`` stamps the recording tool as ``"chisurf"`` by default;
    for a host-neutral workflow the source was registered by MMFDB itself, and a
    published methods report must not claim a tool that never ran.
    """
    op_id = _producing_operation(db, artifact_id)
    if not op_id:
        return
    with db.transaction():
        db.conn.execute(
            "UPDATE mmfdb_operation SET software_package=? WHERE operation_id=?",
            ("mmfdb", op_id),
        )


def _producing_operation(db: MMFDBClientBase, artifact_id: str) -> str | None:
    row = db.conn.execute(
        "SELECT operation_id FROM mmfdb_operation_artifact "
        "WHERE artifact_id=? AND direction='output' LIMIT 1",
        (artifact_id,),
    ).fetchone()
    return row[0] if row else None


def _resolve(base_dir: Path, path: str) -> Path:
    candidate = Path(path).expanduser()
    return candidate if candidate.is_absolute() else (base_dir / candidate)


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()
