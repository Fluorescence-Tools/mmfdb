"""Every shipped example workflow must validate; the self-contained ones must run.

This keeps ``examples/workflows/`` honest: the YAML the docs point at is parsed
and topologically ordered on every test run, and the pure-Python examples are
executed end to end against a temporary database and exported, so a broken tool,
data file, or reference is caught here rather than by a user.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mmfdb.provenance.result_registry import set_global_db
from mmfdb.repository import MFDatabase
from mmfdb.workflow import export_publication, load_workflow, run_workflow

WF_DIR = Path(__file__).resolve().parents[1] / "examples" / "workflows"
ALL_WORKFLOWS = sorted(WF_DIR.glob("*.yaml"))


def _resolve(base_dir: Path, path: str) -> Path:
    candidate = Path(path).expanduser()
    return candidate if candidate.is_absolute() else (base_dir / candidate)


def _sources_present(path: Path) -> bool:
    wf = load_workflow(path)
    return all(_resolve(wf.base_dir, s.path).is_file() for s in wf.sources.values())


RUNNABLE = [p for p in ALL_WORKFLOWS if _sources_present(p)]


def test_there_are_example_workflows():
    assert ALL_WORKFLOWS, "no example workflows found under examples/workflows"
    assert RUNNABLE, "expected at least one self-contained runnable example"


@pytest.mark.parametrize("path", ALL_WORKFLOWS, ids=lambda p: p.name)
def test_example_workflow_validates(path):
    wf = load_workflow(path)
    assert wf.name
    assert wf.ordered_steps()  # raises on a cycle / unresolved reference


@pytest.mark.parametrize("path", RUNNABLE, ids=lambda p: p.name)
def test_example_workflow_runs_and_publishes(path, tmp_path):
    wf = load_workflow(path)
    db = MFDatabase(str(tmp_path / "db.sqlite"))
    try:
        run = run_workflow(wf, db, workdir=tmp_path / "work")
        assert run.sources
        assert all(step.outputs for step in run.steps)

        if wf.publish is not None:
            assert run.seed_artifact_id
            produced = export_publication(
                db, run.seed_artifact_id, tmp_path / "pub", wf.publish.formats, name=wf.name
            )
            assert set(produced) == set(wf.publish.formats)
            for artifact_path in produced.values():
                assert Path(artifact_path).is_file()
    finally:
        set_global_db(None)
        db.close()
