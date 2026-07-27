"""Step executors: run one workflow step and report how it was invoked.

Two host-neutral executors let a workflow stitch together *anything*:

* :class:`SubprocessExecutor` runs a command-line tool (``cmd:``) as a real
  subprocess, capturing the exact argv, exit code, and stdout.
* :class:`PythonExecutor` imports and calls a Python entry point
  (``python: module:callable``), passing a :class:`StepContext` and capturing
  whatever summary dict the callable returns.

Neither executor writes to the database; they only run the tool and return an
:class:`Invocation`. The :mod:`mmfdb.workflow.runner` turns that into provenance.
"""

from __future__ import annotations

import importlib
import json
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from mmfdb.workflow.spec import StepSpec

_PLACEHOLDER = re.compile(r"\{(inputs|outputs|params|workdir|workflow_dir)(?:\.([A-Za-z0-9_]+))?\}")
_STDOUT_LIMIT = 4000


class ExecutorError(RuntimeError):
    """A step failed to run (nonzero exit, missing callable, bad template)."""


@dataclass
class StepContext:
    """Everything a step needs to run: resolved input/output paths and params."""

    step_id: str
    inputs: dict[str, Path]  # local input name -> existing file path
    outputs: dict[str, Path]  # output name -> path the step must write
    params: dict[str, Any]
    workdir: Path
    workflow_dir: Path = Path(".")  # directory of the workflow file (for tool paths)


@dataclass
class Invocation:
    """How a step ran — recorded verbatim on the operation."""

    kind: str  # "cmd" or "python"
    command_line: str
    #: Process exit status; ``None`` for an in-process (``python``) step.
    returncode: int | None = None
    stdout: str = ""
    summary: dict[str, Any] = field(default_factory=dict)


class SubprocessExecutor:
    """Run a ``cmd:`` template as a subprocess in the run's working directory."""

    kind = "cmd"

    def execute(self, step: StepSpec, ctx: StepContext) -> Invocation:
        if step.cmd is None:  # pragma: no cover - guarded by the runner
            raise ExecutorError(f"step {step.id!r} has no 'cmd'")
        command_line = _render(step.cmd, ctx)
        argv = shlex.split(command_line)
        if not argv:
            raise ExecutorError(f"step {step.id!r} 'cmd' rendered empty")
        proc = subprocess.run(
            argv,
            cwd=str(ctx.workdir),
            capture_output=True,
            text=True,
        )
        stdout = (proc.stdout or "")[:_STDOUT_LIMIT]
        if proc.returncode != 0:
            stderr = (proc.stderr or "")[:_STDOUT_LIMIT]
            raise ExecutorError(
                f"step {step.id!r} exited {proc.returncode}: {stderr.strip() or stdout.strip()}"
            )
        return Invocation(
            kind="cmd",
            command_line=command_line,
            returncode=proc.returncode,
            stdout=stdout,
            summary=_maybe_json(stdout),
        )


class PythonExecutor:
    """Import ``module:callable`` and call it with the :class:`StepContext`.

    The callable is expected to write its declared output files and may return a
    JSON-serializable summary dict, which is recorded in the operation settings.
    """

    kind = "python"

    def execute(self, step: StepSpec, ctx: StepContext) -> Invocation:
        if step.python is None:  # pragma: no cover - guarded by the runner
            raise ExecutorError(f"step {step.id!r} has no 'python'")
        func = _import_callable(step.python, ctx.workflow_dir)
        result = func(ctx)
        summary = dict(result) if isinstance(result, dict) else {}
        return Invocation(
            kind="python",
            command_line=step.python,
            returncode=None,  # in-process: there is no process to exit
            stdout="",
            summary=summary,
        )


def default_executors() -> dict[str, Any]:
    """The built-in executor registry keyed by step kind (``cmd``/``python``)."""
    return {"cmd": SubprocessExecutor(), "python": PythonExecutor()}


def _render(template: str, ctx: StepContext) -> str:
    """Expand ``{inputs.x}`` / ``{outputs.y}`` / ``{params.z}`` / ``{workdir}``."""

    def replace(match: re.Match[str]) -> str:
        namespace, key = match.group(1), match.group(2)
        if namespace == "workdir":
            return str(ctx.workdir)
        if namespace == "workflow_dir":
            return str(ctx.workflow_dir)
        table = {"inputs": ctx.inputs, "outputs": ctx.outputs, "params": ctx.params}[namespace]
        if key is None or key not in table:
            raise ExecutorError(f"unknown placeholder {match.group(0)} in step {ctx.step_id!r}")
        value = table[key]
        return str(value)

    return _PLACEHOLDER.sub(replace, template)


def _import_callable(target: str, base_dir: Path) -> Callable[[StepContext], Any]:
    """Resolve ``module:callable`` or ``path/to/file.py:callable``.

    A dotted module name is imported from ``sys.path``; a target whose module
    part looks like a file path (ends in ``.py`` or contains a separator) is
    loaded from that file, resolved relative to the workflow's directory. The
    file form lets an example ship its adapter next to the YAML without any
    installation or ``PYTHONPATH`` setup.
    """
    module_part, sep, attr = target.partition(":")
    if not sep or not attr:
        raise ExecutorError(f"python target {target!r} must be 'module:callable'")

    if module_part.endswith(".py") or "/" in module_part or "\\" in module_part:
        module = _load_module_from_file(module_part, base_dir)
    else:
        try:
            module = importlib.import_module(module_part)
        except ImportError as exc:
            raise ExecutorError(f"cannot import {module_part!r}: {exc}") from exc

    func = getattr(module, attr, None)
    if not callable(func):
        raise ExecutorError(f"{target!r} is not a callable")
    return func


def _load_module_from_file(module_path: str, base_dir: Path):
    import importlib.util

    path = Path(module_path).expanduser()
    if not path.is_absolute():
        path = (base_dir / path).resolve()
    if not path.is_file():
        raise ExecutorError(f"python target file not found: {path}")
    spec = importlib.util.spec_from_file_location(path.stem, str(path))
    if spec is None or spec.loader is None:
        raise ExecutorError(f"cannot load python module from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _maybe_json(text: str) -> dict[str, Any]:
    """Parse a tool's stdout as a JSON summary, or return an empty dict."""
    text = text.strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}
