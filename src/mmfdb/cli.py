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


if __name__ == "__main__":
    cli()
