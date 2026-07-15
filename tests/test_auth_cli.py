"""Headless CLI tests for `mmfdb-admin auth …` (PRD-59 Phase 3)."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner
from mmfdb.admin.cli import cli
from mmfdb.repository import MFDatabase


def _prepare_db(tmp_path: Path, monkeypatch) -> Path:
    db_path = tmp_path / "cli_auth.db"
    monkeypatch.setenv("MMFDB_BOOTSTRAP_ADMIN_USER", "cli-admin")
    monkeypatch.setenv("MMFDB_BOOTSTRAP_ADMIN_PASSWORD", "Cli-admin1!")
    MFDatabase(db_path).close()
    monkeypatch.setattr(
        "mmfdb.store.database_resolver.resolve_database_path",
        lambda *a, **k: db_path,
    )
    # Deterministic provider config (local) regardless of ambient env/resolver.
    import mmfdb.config as config

    monkeypatch.setattr(config, "_AUTH_CONFIG_RESOLVER", None)
    monkeypatch.delenv("MMFDB_AUTH_PROVIDER", raising=False)
    return db_path


def test_cli_auth_login_and_whoami(tmp_path: Path, monkeypatch) -> None:
    _prepare_db(tmp_path, monkeypatch)
    runner = CliRunner()

    r = runner.invoke(
        cli, ["auth", "login", "--user", "cli-admin", "--password", "Cli-admin1!", "--json"]
    )
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert data["ok"] and data["authenticated"]
    token = data["token"]
    assert data["user"]["user_id"] == "cli-admin"

    r2 = runner.invoke(cli, ["auth", "whoami", "--token", token, "--json"])
    assert r2.exit_code == 0, r2.output
    who = json.loads(r2.output)
    assert who["authenticated"] is True
    assert who["user_id"] == "cli-admin"
    assert who["is_admin"] is True


def test_cli_auth_login_bad_password_aborts(tmp_path: Path, monkeypatch) -> None:
    _prepare_db(tmp_path, monkeypatch)
    runner = CliRunner()
    r = runner.invoke(
        cli, ["auth", "login", "--user", "cli-admin", "--password", "wrong"]
    )
    assert r.exit_code != 0
    assert "Login failed" in r.output


def test_cli_auth_whoami_invalid_token(tmp_path: Path, monkeypatch) -> None:
    _prepare_db(tmp_path, monkeypatch)
    runner = CliRunner()
    r = runner.invoke(cli, ["auth", "whoami", "--token", "not-a-real-token"])
    assert r.exit_code == 0
    assert "anonymous" in r.output


def test_cli_auth_status_defaults_local(tmp_path: Path, monkeypatch) -> None:
    _prepare_db(tmp_path, monkeypatch)
    runner = CliRunner()
    r = runner.invoke(cli, ["auth", "status", "--json"])
    assert r.exit_code == 0, r.output
    status = json.loads(r.output)
    assert status["provider"] == "local"
    assert status["local_always_available"] is True


def test_cli_bootstrap_admin_is_one_shot_and_never_echoes_password(
    tmp_path: Path, monkeypatch
) -> None:
    db_path = tmp_path / "bootstrap.db"
    monkeypatch.delenv("MMFDB_BOOTSTRAP_ADMIN_USER", raising=False)
    monkeypatch.delenv("MMFDB_BOOTSTRAP_ADMIN_PASSWORD", raising=False)
    MFDatabase(db_path).close()
    monkeypatch.setattr(
        "mmfdb.store.database_resolver.resolve_database_path", lambda *a, **k: db_path
    )
    runner = CliRunner()
    password = "Local-admin1!"

    created = runner.invoke(
        cli,
        ["auth", "bootstrap-admin", "--user", "local-admin", "--password", password, "--json"],
    )
    assert created.exit_code == 0, created.output
    assert password not in created.output
    assert json.loads(created.output) == {
        "ok": True,
        "user_id": "local-admin",
        "is_admin": True,
    }

    repeated = runner.invoke(
        cli,
        ["auth", "bootstrap-admin", "--user", "other", "--password", "Other-admin1!"],
    )
    assert repeated.exit_code != 0
    assert "already has an active administrator" in repeated.output
