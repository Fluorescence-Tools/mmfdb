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


@cli.group("export")
def export() -> None:
    """Export recorded metadata out of MMFDB in exchange formats."""


@export.command("cif")
@click.option("--analysis", "analysis_id", default=None,
              help="Analysis to export (default: the first one).")
@click.option("--output", "output_path", default=None, type=click.Path(dir_okay=False),
              help="Write the CIF here instead of to stdout.")
@click.option("--no-extension", is_flag=True, help="Emit only standard PDBx/FLR categories.")
@click.option("--token", envvar="MMFDB_TOKEN", default=None,
              help="Session token (see `mmfdb-admin auth login`).")
def export_cif(
    analysis_id: str | None, output_path: str | None, no_extension: bool, token: str | None
) -> None:
    """Export one analysis and its sample description as an mmCIF document."""
    from mmfdb.api import export_cif as export_cif_api
    from mmfdb.security.auth import AuthError, PermissionDenied

    try:
        result = export_cif_api(
            analysis_id=analysis_id,
            output_path=output_path,
            include_extension=not no_extension,
            auth={"token": token} if token else None,
        )
    except (AuthError, PermissionDenied) as exc:
        raise click.ClickException(str(exc)) from exc
    if output_path:
        click.echo(f"{result['analysis_id']}: {result['output_path']}")
    else:
        click.echo(result["text"], nl=False)


@cli.group("project")
def project() -> None:
    """Move a ChiSurf ``.pto`` / ``.cs.pto`` between its container and mmCIF."""


@project.command("export-cif")
@click.argument("pto_path", type=click.Path(exists=True, dir_okay=False))
@click.option("--output", "output_path", default=None, type=click.Path(dir_okay=False),
              help="Write the CIF here instead of to stdout.")
def project_export_cif(pto_path: str, output_path: str | None) -> None:
    """Export the provenance of a ``.pto`` (measurement or ``.cs.pto`` project) as mmCIF."""
    from mmfdb.provenance.pto_graph import graph_from_pto, write_graph_cif

    try:
        text = write_graph_cif(graph_from_pto(pto_path), output_path)
    except (OSError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    if output_path:
        click.echo(output_path)
    else:
        click.echo(text, nl=False)


@project.command("import-cif")
@click.argument("cif_path", type=click.Path(exists=True, dir_okay=False))
@click.option("--database", default=None, help="Database path or URL (default: the configured one).")
def project_import_cif(cif_path: str, database: str | None) -> None:
    """Record the provenance graph of an exported mmCIF in the database."""
    from mmfdb.provenance.pto_graph import import_graph, read_graph_cif

    graph = read_graph_cif(cif_path)
    try:
        with MFDatabase(_database_target(database)) as db:
            import_graph(db, graph)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps({k: len(v) for k, v in graph.items()}))


@cli.group("eln")
def eln() -> None:
    """Exchange chemicals, instruments and deposits with an electronic lab notebook."""


def _eln_gateway(url: str, user: str, collection: int | None):
    """Build the Chemotion gateway with the token from the credential store or environment."""
    from mmfdb.eln import ChemotionGateway, credentials

    token = credentials.load_token("chemotion", url, user)
    if not token:
        raise click.ClickException(
            "no token: store one with `mmfdb eln login` or set MMFDB_CHEMOTION_TOKEN")
    try:
        return ChemotionGateway(url, token, collection_id=collection)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc


_ELN_OPTIONS = [
    click.option("--url", required=True, help="Chemotion server root, https://eln.example"),
    click.option("--user", default="default", show_default=True, help="Account name of the stored token."),
    click.option("--collection", type=int, default=None, help="Collection id to read from or deposit into."),
]


def _eln_options(fn):
    for option in reversed(_ELN_OPTIONS):
        fn = option(fn)
    return fn


@eln.command("login")
@click.option("--url", required=True)
@click.option("--user", default="default", show_default=True)
@click.password_option("--token", prompt="Chemotion token", confirmation_prompt=False)
def eln_login(url: str, user: str, token: str) -> None:
    """Store a Chemotion token in the OS credential store."""
    from mmfdb.eln import credentials

    if not credentials.store_token("chemotion", url, user, token):
        raise click.ClickException(
            "no credential store accepted the token; set MMFDB_CHEMOTION_TOKEN instead")
    click.echo("token stored")


@eln.command("pull")
@_eln_options
@click.option("--database", default=None)
@click.option("--kinds", default="chemicals,instruments", show_default=True)
def eln_pull(url: str, user: str, collection: int | None, database: str | None, kinds: str) -> None:
    """Import chemicals (as reagent lots) and instruments (as setups) from the ELN."""
    from mmfdb.eln import ElnUnavailable, pull

    gateway = _eln_gateway(url, user, collection)
    try:
        with MFDatabase(_database_target(database)) as db:
            report = pull(gateway, db, tuple(k.strip() for k in kinds.split(",") if k.strip()))
    except ElnUnavailable as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps(report))


@eln.command("deposit")
@click.argument("pto_path", type=click.Path(exists=True, dir_okay=False))
@_eln_options
@click.option("--title", default=None)
def eln_deposit(pto_path: str, url: str, user: str, collection: int | None, title: str | None) -> None:
    """Deposit a ``.pto`` / ``.cs.pto`` with its mmCIF provenance as an ELN record."""
    from mmfdb.eln import ElnUnavailable, deposit_pto

    gateway = _eln_gateway(url, user, collection)
    try:
        ref = deposit_pto(gateway, pto_path, title=title)
    except (ElnUnavailable, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(ref.url or ref.remote_id)


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
