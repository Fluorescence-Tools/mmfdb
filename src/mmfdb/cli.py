"""Standalone MMFDB deployment commands."""

from __future__ import annotations

import json
import os
from pathlib import Path

import click

from mmfdb.config import (
    ConfigError,
    DeploymentConfig,
    apply_deployment_config,
    load_deployment_config,
)
from mmfdb.repository import MFDatabase
from mmfdb.security.bootstrap import AdminBootstrapResult, ensure_configured_admin
from mmfdb.store.database_resolver import resolve_database_path


def initialize_deployment(config: DeploymentConfig) -> AdminBootstrapResult:
    """Initialize storage and reconcile the one-shot administrator bootstrap."""
    apply_deployment_config(config)
    target = config.database.url or config.database.path or resolve_database_path()
    if isinstance(target, Path):
        target.parent.mkdir(parents=True, exist_ok=True)
    with MFDatabase(target) as database:
        return ensure_configured_admin(database.conn, config.admin)


def _load(path: str | None) -> DeploymentConfig:
    try:
        return load_deployment_config(path)
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from exc


@click.group()
def cli() -> None:
    """Run and initialize a standalone MMFDB service."""


@cli.command("check-config")
@click.option("--config", "config_path", envvar="MMFDB_CONFIG", type=click.Path())
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable output.")
def check_config(config_path: str | None, as_json: bool) -> None:
    """Validate YAML without opening the database or changing state."""
    config = _load(config_path)
    result = {
        "ok": True,
        "version": config.version,
        "mode": config.mode,
        "client_mode": config.client.mode,
        "database": os.fspath(config.database.path) if config.database.path else config.database.url,
        "server": {"host": config.server.host, "port": config.server.port},
    }
    click.echo(json.dumps(result) if as_json else "MMFDB configuration is valid")


@cli.command("init")
@click.option("--config", "config_path", envvar="MMFDB_CONFIG", type=click.Path())
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable output.")
def init(config_path: str | None, as_json: bool) -> None:
    """Initialize a configured database and its first administrator."""
    config = _load(config_path)
    try:
        result = initialize_deployment(config)
    except (OSError, RuntimeError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    payload = {"ok": True, "user_id": result.user_id, "created": result.created}
    if as_json:
        click.echo(json.dumps(payload))
    elif result.created:
        click.echo(f"Initialized MMFDB and created administrator {result.user_id!r}")
    else:
        click.echo(f"MMFDB is initialized; existing administrator {result.user_id!r} preserved")


@cli.command("serve")
@click.option("--config", "config_path", envvar="MMFDB_CONFIG", type=click.Path())
@click.option("--host", default=None, help="Override server.host from YAML.")
@click.option("--port", default=None, type=click.IntRange(1, 65535), help="Override server.port.")
def serve(config_path: str | None, host: str | None, port: int | None) -> None:
    """Initialize and run the standalone MMFDB HTTP/web-admin service."""
    config = _load(config_path)
    if config.mode != "standalone":
        raise click.ClickException("serve requires mode: standalone in the MMFDB YAML")
    try:
        result = initialize_deployment(config)
    except (OSError, RuntimeError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    action = "created" if result.created else "preserved"
    click.echo(f"MMFDB administrator {result.user_id!r} {action}", err=True)

    try:
        from mmfdb.webadmin import serve as serve_webadmin
    except ImportError as exc:
        raise click.ClickException("MMFDB web-admin server is not installed") from exc
    serve_webadmin(host=host or config.server.host, port=port or config.server.port)


@cli.group("workflow")
def workflow() -> None:
    """Define, run, and export YAML-declared analysis workflows."""


def _database_target(database: str | None) -> str:
    return database or os.fspath(resolve_database_path())


@workflow.command("validate")
@click.argument("workflow_path", type=click.Path(exists=True, dir_okay=False))
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable output.")
def workflow_validate(workflow_path: str, as_json: bool) -> None:
    """Load and validate a workflow YAML without running anything."""
    from mmfdb.workflow.spec import WorkflowError, load_workflow

    try:
        wf = load_workflow(workflow_path)
    except WorkflowError as exc:
        raise click.ClickException(str(exc)) from exc
    steps = [s.id for s in wf.ordered_steps()]
    if as_json:
        click.echo(json.dumps({"ok": True, "name": wf.name, "steps": steps}))
    else:
        click.echo(f"Workflow {wf.name!r} is valid: {len(steps)} step(s) [{' -> '.join(steps)}]")


@workflow.command("run")
@click.argument("workflow_path", type=click.Path(exists=True, dir_okay=False))
@click.option("--database", default=None, help="SQLite path or database URL (default: configured).")
@click.option("--workdir", default=None, type=click.Path(file_okay=False), help="Where step outputs are written.")
@click.option("--output-dir", "output_dir", default=None, type=click.Path(file_okay=False),
              help="Write the workflow's publish outputs here (defaults to the workflow's publish block).")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable output.")
def workflow_run(
    workflow_path: str, database: str | None, workdir: str | None, output_dir: str | None, as_json: bool
) -> None:
    """Execute a workflow, recording provenance, and export its publish outputs."""
    from mmfdb.workflow.export import export_publication
    from mmfdb.workflow.runner import WorkflowRunError, run_workflow
    from mmfdb.workflow.spec import WorkflowError, load_workflow

    try:
        wf = load_workflow(workflow_path)
    except WorkflowError as exc:
        raise click.ClickException(str(exc)) from exc

    exports: dict[str, str] = {}
    with MFDatabase(_database_target(database)) as db:
        try:
            run = run_workflow(wf, db, workdir=workdir)
        except (WorkflowRunError, WorkflowError) as exc:
            raise click.ClickException(str(exc)) from exc
        if wf.publish is not None and run.seed_artifact_id:
            out = output_dir or str((run.workdir or wf.base_dir))
            exports = export_publication(
                db, run.seed_artifact_id, out, wf.publish.formats, name=wf.name,
                workflow=wf, workflow_yaml=Path(workflow_path).read_text(encoding="utf-8"),
            )

    payload = {
        "ok": True,
        "name": wf.name,
        "sources": run.sources,
        "steps": {s.step_id: s.outputs for s in run.steps},
        "seed_artifact_id": run.seed_artifact_id,
        "exports": exports,
    }
    if as_json:
        click.echo(json.dumps(payload))
        return
    click.echo(f"Ran workflow {wf.name!r}: {len(run.steps)} step(s), {len(run.sources)} source(s)")
    for name, path in exports.items():
        click.echo(f"  {name}: {path}")


@workflow.command("export")
@click.argument("workflow_path", type=click.Path(exists=True, dir_okay=False))
@click.option("--seed", "seed_artifact_id", required=True, help="Artifact id to seed the provenance graph.")
@click.option("--output-dir", "output_dir", required=True, type=click.Path(file_okay=False))
@click.option("--database", default=None, help="SQLite path or database URL (default: configured).")
@click.option("--format", "formats", multiple=True, help="report | mmcif | bundle (repeatable).")
def workflow_export(
    workflow_path: str, seed_artifact_id: str, output_dir: str, database: str | None, formats: tuple[str, ...]
) -> None:
    """Re-export publication artifacts from a previously recorded run."""
    from mmfdb.workflow.export import export_publication
    from mmfdb.workflow.spec import WorkflowError, load_workflow

    try:
        wf = load_workflow(workflow_path)
    except WorkflowError as exc:
        raise click.ClickException(str(exc)) from exc
    chosen = list(formats) or (list(wf.publish.formats) if wf.publish else ["report"])
    with MFDatabase(_database_target(database)) as db:
        produced = export_publication(
            db, seed_artifact_id, output_dir, chosen, name=wf.name,
            workflow=wf, workflow_yaml=Path(workflow_path).read_text(encoding="utf-8"),
        )
    for name, path in produced.items():
        click.echo(f"{name}: {path}")


@workflow.command("extract")
@click.argument("cif_path", type=click.Path(exists=True, dir_okay=False))
@click.option("--output-dir", "output_dir", required=True, type=click.Path(file_okay=False))
def workflow_extract(cif_path: str, output_dir: str) -> None:
    """Unpack a single deposit CIF: recover its data files and documents."""
    from mmfdb.workflow.export import read_single_cif

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    extracted = read_single_cif(cif_path)
    written = []
    for name, data in extracted["files"].items():
        (out / name).write_bytes(data)
        written.append(name)
    for name, text in extracted["documents"].items():
        (out / name).write_text(text, encoding="utf-8")
        written.append(name)
    click.echo(f"Extracted {len(written)} item(s) to {out}:")
    for name in sorted(written):
        click.echo(f"  {name}")


if __name__ == "__main__":
    cli()
