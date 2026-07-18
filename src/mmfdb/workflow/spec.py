"""Declarative workflow specification: parse and validate a plain-YAML pipeline.

A workflow is a small YAML document that names its raw **sources**, a list of
**steps** that each run one tool (a command-line program or a Python callable),
and an optional **publish** block describing what to export for a paper. This
module turns that document into validated, immutable dataclasses and computes a
topological execution order. It performs **no** execution and imports nothing
from a consumer application — running the steps and recording provenance is the
job of :mod:`mmfdb.workflow.runner`.

The schema (version 1)::

    version: 1
    name: smfret-burst-analysis
    description: Stitch a tttrlib CLI and FRETBursts on one measurement.

    sources:
      raw:                        # source name, referenced as "raw"
        path: data/measure.spc
        kind: raw_measurement     # optional; defaults to raw_measurement
        metadata: {file_type: SPC-130}

    steps:
      - id: burst_cli
        operation_type: burst_selection
        software: {package: fret_burst_tool, version: tttrlib-0.27}
        inputs: {photons: raw}    # local name -> ref (a source or "step.output")
        params: {min_photons: 20}
        cmd: >                     # a command-line tool, run as a subprocess
          python fret_burst_tool.py {inputs.photons}
          --min-photons {params.min_photons} --output {outputs.bursts}
        outputs:
          bursts: {kind: burst_table, path: bursts_cli.csv}

      - id: burst_fb
        operation_type: burst_selection
        software: {package: FRETBursts, version: 0.8.3}
        inputs: {photons: raw}
        params: {F: 6, m: 10}
        python: my_adapters:run_fretbursts   # a Python callable(ctx)
        outputs:
          bursts: {kind: burst_table, path: bursts_fb.csv}

    publish:
      seed: raw                    # provenance graph is exported from this node
      formats: [report, mmcif, bundle]
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

SCHEMA_VERSION = 1

_TOP_LEVEL_KEYS = {"version", "name", "description", "sources", "steps", "publish"}
_SOURCE_KEYS = {"path", "kind", "metadata"}
_STEP_KEYS = {"id", "operation_type", "software", "inputs", "params", "cmd", "python", "outputs"}
_OUTPUT_KEYS = {"kind", "path"}
_PUBLISH_KEYS = {"seed", "formats"}
_KNOWN_FORMATS = {"report", "mmcif", "bundle", "archive", "cif"}


class WorkflowError(ValueError):
    """A workflow document is malformed or internally inconsistent.

    Raised entirely from parsing/validation, before any source is registered or
    any step is executed, so a rejected workflow never touches the database.
    """


@dataclass(frozen=True)
class Software:
    """The tool a step runs, recorded on the operation for citation."""

    package: str = ""
    version: str = ""


@dataclass(frozen=True)
class SourceSpec:
    """A raw input file registered into MMFDB before any step runs."""

    name: str
    path: str
    kind: str = "raw_measurement"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OutputSpec:
    """One artifact a step produces."""

    name: str
    kind: str = "processed_data"
    path: str = ""  # relative to the run's working directory; auto-named when ""


@dataclass(frozen=True)
class StepSpec:
    """One tool invocation: a subprocess (``cmd``) or a Python callable (``python``)."""

    id: str
    operation_type: str = "analysis"
    software: Software = field(default_factory=Software)
    inputs: dict[str, str] = field(default_factory=dict)  # local name -> ref
    params: dict[str, Any] = field(default_factory=dict)
    outputs: dict[str, OutputSpec] = field(default_factory=dict)
    cmd: str | None = None
    python: str | None = None

    @property
    def kind(self) -> str:
        """``"cmd"`` for a subprocess step, ``"python"`` for a callable step."""
        return "cmd" if self.cmd is not None else "python"


@dataclass(frozen=True)
class PublishSpec:
    """What to export for publication once the workflow has run."""

    seed: str
    formats: tuple[str, ...] = ("report",)


@dataclass(frozen=True)
class Workflow:
    """A validated, immutable workflow with a computed execution order."""

    name: str
    version: int
    description: str
    sources: dict[str, SourceSpec]
    steps: tuple[StepSpec, ...]
    publish: PublishSpec | None
    base_dir: Path

    def step(self, step_id: str) -> StepSpec:
        """Return the step with ``step_id`` or raise ``KeyError``."""
        for step in self.steps:
            if step.id == step_id:
                return step
        raise KeyError(step_id)

    def ordered_steps(self) -> tuple[StepSpec, ...]:
        """Steps in a valid execution order (dependencies first)."""
        order = _topological_order(self.steps)
        by_id = {s.id: s for s in self.steps}
        return tuple(by_id[i] for i in order)


def load_workflow(path: str | os.PathLike[str]) -> Workflow:
    """Load and validate a workflow from a YAML file.

    Source paths and step working paths resolve relative to the file's
    directory. Raises :class:`WorkflowError` for any structural problem.
    """
    selected = Path(path).expanduser()
    try:
        text = selected.read_text(encoding="utf-8")
    except OSError as exc:
        raise WorkflowError(f"cannot read workflow {selected}: {exc}") from exc
    return load_workflow_text(text, base_dir=selected.resolve().parent)


def load_workflow_text(text: str, base_dir: str | os.PathLike[str] = ".") -> Workflow:
    """Load and validate a workflow from a YAML string.

    ``base_dir`` anchors relative source paths. Raises :class:`WorkflowError`.
    """
    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise WorkflowError(f"invalid YAML: {exc}") from exc
    if not isinstance(loaded, dict):
        raise WorkflowError("workflow must be a YAML mapping")
    return _build(loaded, Path(base_dir).resolve())


def _build(doc: dict[str, Any], base_dir: Path) -> Workflow:
    _reject_unknown(doc, _TOP_LEVEL_KEYS, "workflow")

    version = doc.get("version")
    if version != SCHEMA_VERSION:
        raise WorkflowError(f"unsupported workflow version {version!r}; expected {SCHEMA_VERSION}")

    name = str(doc.get("name") or "workflow")
    description = str(doc.get("description") or "")

    sources = _build_sources(doc.get("sources") or {})
    steps = _build_steps(doc.get("steps") or [])
    publish = _build_publish(doc.get("publish"))

    workflow = Workflow(
        name=name,
        version=version,
        description=description,
        sources=sources,
        steps=steps,
        publish=publish,
        base_dir=base_dir,
    )
    _validate_references(workflow)
    _topological_order(steps)  # raises on cycle
    return workflow


def _build_sources(raw: Any) -> dict[str, SourceSpec]:
    if not isinstance(raw, dict):
        raise WorkflowError("'sources' must be a mapping of name -> source")
    sources: dict[str, SourceSpec] = {}
    for src_name, body in raw.items():
        if not isinstance(body, dict):
            raise WorkflowError(f"source {src_name!r} must be a mapping")
        _reject_unknown(body, _SOURCE_KEYS, f"source {src_name!r}")
        path = body.get("path")
        if not path:
            raise WorkflowError(f"source {src_name!r} needs a 'path'")
        metadata = body.get("metadata") or {}
        if not isinstance(metadata, dict):
            raise WorkflowError(f"source {src_name!r} metadata must be a mapping")
        sources[str(src_name)] = SourceSpec(
            name=str(src_name),
            path=str(path),
            kind=str(body.get("kind") or "raw_measurement"),
            metadata=dict(metadata),
        )
    return sources


def _build_steps(raw: Any) -> tuple[StepSpec, ...]:
    if not isinstance(raw, list):
        raise WorkflowError("'steps' must be a list")
    steps: list[StepSpec] = []
    seen_ids: set[str] = set()
    for index, body in enumerate(raw):
        if not isinstance(body, dict):
            raise WorkflowError(f"step #{index} must be a mapping")
        _reject_unknown(body, _STEP_KEYS, f"step #{index}")
        step_id = body.get("id")
        if not step_id:
            raise WorkflowError(f"step #{index} needs an 'id'")
        step_id = str(step_id)
        if step_id in seen_ids:
            raise WorkflowError(f"duplicate step id {step_id!r}")
        seen_ids.add(step_id)

        cmd = body.get("cmd")
        python = body.get("python")
        if (cmd is None) == (python is None):
            raise WorkflowError(f"step {step_id!r} must have exactly one of 'cmd' or 'python'")

        inputs = body.get("inputs") or {}
        if not isinstance(inputs, dict):
            raise WorkflowError(f"step {step_id!r} 'inputs' must be a mapping of name -> ref")
        params = body.get("params") or {}
        if not isinstance(params, dict):
            raise WorkflowError(f"step {step_id!r} 'params' must be a mapping")

        software = _build_software(body.get("software"), step_id)
        outputs = _build_outputs(body.get("outputs") or {}, step_id)

        steps.append(
            StepSpec(
                id=step_id,
                operation_type=str(body.get("operation_type") or "analysis"),
                software=software,
                inputs={str(k): str(v) for k, v in inputs.items()},
                params=dict(params),
                outputs=outputs,
                cmd=str(cmd) if cmd is not None else None,
                python=str(python) if python is not None else None,
            )
        )
    return tuple(steps)


def _build_software(raw: Any, step_id: str) -> Software:
    if raw is None:
        return Software()
    if not isinstance(raw, dict):
        raise WorkflowError(f"step {step_id!r} 'software' must be a mapping")
    return Software(package=str(raw.get("package") or ""), version=str(raw.get("version") or ""))


def _build_outputs(raw: Any, step_id: str) -> dict[str, OutputSpec]:
    if not isinstance(raw, dict):
        raise WorkflowError(f"step {step_id!r} 'outputs' must be a mapping of name -> output")
    outputs: dict[str, OutputSpec] = {}
    for out_name, body in raw.items():
        body = body or {}
        if not isinstance(body, dict):
            raise WorkflowError(f"step {step_id!r} output {out_name!r} must be a mapping")
        _reject_unknown(body, _OUTPUT_KEYS, f"step {step_id!r} output {out_name!r}")
        outputs[str(out_name)] = OutputSpec(
            name=str(out_name),
            kind=str(body.get("kind") or "processed_data"),
            path=str(body.get("path") or ""),
        )
    return outputs


def _build_publish(raw: Any) -> PublishSpec | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise WorkflowError("'publish' must be a mapping")
    _reject_unknown(raw, _PUBLISH_KEYS, "publish")
    seed = raw.get("seed")
    if not seed:
        raise WorkflowError("'publish' needs a 'seed' reference")
    formats = raw.get("formats") or ["report"]
    if not isinstance(formats, list):
        raise WorkflowError("'publish.formats' must be a list")
    unknown = {str(f) for f in formats} - _KNOWN_FORMATS
    if unknown:
        raise WorkflowError(
            f"unknown publish formats {sorted(unknown)}; known: {sorted(_KNOWN_FORMATS)}"
        )
    return PublishSpec(seed=str(seed), formats=tuple(str(f) for f in formats))


def parse_ref(ref: str) -> tuple[str, str | None]:
    """Split a reference into ``(source_name, None)`` or ``(step_id, output_name)``."""
    if "." in ref:
        step_id, _, out_name = ref.partition(".")
        return step_id, out_name
    return ref, None


def _validate_references(workflow: Workflow) -> None:
    """Every input ref and the publish seed must resolve to a defined node."""
    collisions = set(workflow.sources) & {s.id for s in workflow.steps}
    if collisions:
        raise WorkflowError(f"names used as both source and step id: {sorted(collisions)}")

    def resolve(ref: str, where: str) -> None:
        base, out_name = parse_ref(ref)
        if out_name is None:
            if base not in workflow.sources:
                raise WorkflowError(f"{where} references unknown source {ref!r}")
            return
        try:
            step = workflow.step(base)
        except KeyError:
            raise WorkflowError(f"{where} references unknown step {base!r}") from None
        if out_name not in step.outputs:
            raise WorkflowError(f"{where} references unknown output {ref!r} of step {base!r}")

    for step in workflow.steps:
        for local_name, ref in step.inputs.items():
            resolve(ref, f"step {step.id!r} input {local_name!r}")
    if workflow.publish is not None:
        resolve(workflow.publish.seed, "publish 'seed'")


def _topological_order(steps: tuple[StepSpec, ...]) -> list[str]:
    """Return step ids in dependency order; raise on a cycle (Kahn's algorithm)."""
    ids = [s.id for s in steps]
    deps: dict[str, set[str]] = {s.id: set() for s in steps}
    for step in steps:
        for ref in step.inputs.values():
            base, out_name = parse_ref(ref)
            if out_name is not None and base in deps:
                deps[step.id].add(base)

    order: list[str] = []
    ready = [i for i in ids if not deps[i]]
    remaining = {i: set(d) for i, d in deps.items()}
    while ready:
        current = ready.pop(0)
        order.append(current)
        for other in ids:
            if current in remaining[other]:
                remaining[other].discard(current)
                if not remaining[other] and other not in order and other not in ready:
                    ready.append(other)
    if len(order) != len(ids):
        cyclic = sorted(set(ids) - set(order))
        raise WorkflowError(f"workflow steps form a cycle: {cyclic}")
    return order


def _reject_unknown(body: dict[str, Any], allowed: set[str], where: str) -> None:
    unknown = set(body) - allowed
    if unknown:
        raise WorkflowError(f"unknown keys in {where}: {sorted(unknown)}")
