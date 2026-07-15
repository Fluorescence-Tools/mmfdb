from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path

import pytest
from click.testing import CliRunner
from mmfdb.admin.backend.password_services import verify_password
from mmfdb.cli import cli
from mmfdb.config import (
    AdminBootstrapConfig,
    ConfigError,
    apply_deployment_config,
    configured_auth_config,
    load_client_config,
    load_deployment_config,
    reset_runtime_config,
    set_auth_config_resolver,
)
from mmfdb.repository import MFDatabase
from mmfdb.security.bootstrap import ensure_configured_admin
from mmfdb.security.login import login


def _write_config(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_yaml_config_resolves_paths_and_secret_environment(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TEST_ADMIN_PASSWORD", "Strong-local1!")
    config_path = _write_config(
        tmp_path / "mmfdb.yaml",
        """
version: 1
mode: standalone
server:
  host: 127.0.0.1
  port: 9876
database:
  path: data/mmfdb.db
object_store:
  backend: local
  root: data/objects
auth:
  provider: local
admin:
  user: admin
  password: ${TEST_ADMIN_PASSWORD}
client:
  base_url: http://127.0.0.1:9876
""",
    )

    config = load_deployment_config(config_path)

    assert config.mode == "standalone"
    assert config.server.host == "127.0.0.1"
    assert config.server.port == 9876
    assert config.database.path == (tmp_path / "data/mmfdb.db").resolve()
    assert config.object_store.root == (tmp_path / "data/objects").resolve()
    assert config.admin.user == "admin"
    assert config.admin.password == "Strong-local1!"
    assert "Strong-local1!" not in repr(config)

    runtime = apply_deployment_config(config)
    assert runtime.database_path == config.database.path
    assert runtime.object_store_root == config.object_store.root


@pytest.mark.parametrize(
    "yaml_text,match",
    [
        ("version: 1\nunknown: true\n", "unknown"),
        ("version: 2\n", "version"),
        ("version: 1\ndatabase:\n  path: a.db\n  url: sqlite:///b.db\n", "both"),
        ("version: 1\nserver:\n  port: 70000\n", "port"),
        ("version: 1\nadmin:\n  user: admin\n  password: admin\n", "weak"),
    ],
)
def test_yaml_config_fails_closed(tmp_path: Path, yaml_text: str, match: str) -> None:
    path = _write_config(tmp_path / "bad.yaml", yaml_text)
    with pytest.raises(ConfigError, match=match):
        load_deployment_config(path)


def test_yaml_config_requires_referenced_environment_variable(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("MISSING_SECRET", raising=False)
    path = _write_config(
        tmp_path / "missing.yaml",
        "version: 1\nadmin:\n  user: admin\n  password: ${MISSING_SECRET}\n",
    )
    with pytest.raises(ConfigError, match="MISSING_SECRET"):
        load_deployment_config(path)


def test_client_loader_does_not_resolve_admin_or_ldap_secrets(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("SERVER_ONLY_ADMIN_SECRET", raising=False)
    path = _write_config(
        tmp_path / "shared.yaml",
        """
version: 1
mode: standalone
server:
  port: 8123
database:
  path: server.db
auth:
  provider: ldap
  ldap:
    bind_password: ${SERVER_ONLY_LDAP_SECRET}
admin:
  user: admin
  password: ${SERVER_ONLY_ADMIN_SECRET}
client:
  mode: remote
  base_url: http://127.0.0.1:8123
  username: admin
""",
    )

    client = load_client_config(path)

    assert client.mode == "remote"
    assert client.base_url == "http://127.0.0.1:8123"
    assert client.username == "admin"


def test_loaded_config_repr_redacts_admin_and_ldap_secrets(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path / "secret.yaml",
        """
version: 1
database:
  path: local.db
auth:
  provider: ldap
  ldap:
    host: ldap.example.org
    bind_password: never-log-this
admin:
  user: admin
  password: Strong-local1!
""",
    )
    config = load_deployment_config(path)
    rendered = repr(config)
    assert "never-log-this" not in rendered
    assert "Strong-local1!" not in rendered


@pytest.mark.parametrize(
    "url",
    [
        "not-a-url",
        "mysql://db.example/mmfdb",
        "postgresql://db.example",
        "postgresql:///mmfdb",
        "sqlite://remote.example/mmfdb.db",
    ],
)
def test_standalone_rejects_malformed_or_unsupported_database_url(
    tmp_path: Path, url: str
) -> None:
    path = _write_config(
        tmp_path / "bad-url.yaml",
        f"version: 1\nmode: standalone\ndatabase:\n  url: {url}\n",
    )
    with pytest.raises(ConfigError, match="database.url"):
        load_deployment_config(path)


def test_standalone_requires_database_target(tmp_path: Path) -> None:
    path = _write_config(tmp_path / "no-db.yaml", "version: 1\nmode: standalone\n")
    with pytest.raises(ConfigError, match="requires exactly one"):
        load_deployment_config(path)


def test_remote_plain_http_requires_explicit_development_override(tmp_path: Path) -> None:
    unsafe = _write_config(
        tmp_path / "unsafe.yaml",
        """
version: 1
client:
  mode: remote
  base_url: http://mmfdb.example.org:8080
""",
    )
    with pytest.raises(ConfigError, match="HTTPS"):
        load_client_config(unsafe)

    allowed = _write_config(
        tmp_path / "allowed.yaml",
        """
version: 1
client:
  mode: remote
  base_url: http://mmfdb.internal:8080
  allow_insecure_http: true
""",
    )
    assert load_client_config(allowed).allow_insecure_http is True


@pytest.mark.parametrize(
    "base_url",
    [
        "https://user:secret@mmfdb.example.org",
        "https://mmfdb.example.org?token=secret",
        "https://mmfdb.example.org/#secret",
    ],
)
def test_client_base_url_rejects_embedded_secrets_and_suffixes(
    tmp_path: Path, base_url: str
) -> None:
    path = _write_config(
        tmp_path / "unsafe-url.yaml",
        f"version: 1\nclient:\n  mode: remote\n  base_url: {base_url}\n",
    )
    with pytest.raises(ConfigError, match="credentials|query string|fragment"):
        load_client_config(path)


def test_reset_runtime_config_clears_host_auth_resolver(monkeypatch) -> None:
    monkeypatch.delenv("MMFDB_AUTH_PROVIDER", raising=False)
    set_auth_config_resolver(lambda: {"auth_provider": "ldap", "ldap": {"host": "old"}})
    assert configured_auth_config()["auth_provider"] == "ldap"
    reset_runtime_config()
    assert configured_auth_config() is None


def test_explicit_local_weak_bootstrap_is_idempotent_and_never_resets_password(
    tmp_path: Path,
) -> None:
    path = _write_config(
        tmp_path / "local.yaml",
        """
version: 1
mode: standalone
database:
  path: local.db
admin:
  user: admin
  password: admin
  allow_weak_bootstrap: true
""",
    )
    config = load_deployment_config(path)
    apply_deployment_config(config)
    with MFDatabase(config.database.path) as db:
        first = ensure_configured_admin(db.conn, config.admin)
        assert first.created is True
        row = db.conn.execute(
            "SELECT is_admin, password_hash FROM flr_sample_users WHERE user_id = 'admin'"
        ).fetchone()
        assert row[0] == 1
        assert verify_password("admin", row[1])
        session = login(db.conn, provider="local", user_id="admin", password="admin")
        assert session["authenticated"] is True
        assert session["user"]["user_id"] == "admin"

        original_hash = row[1]
        second = ensure_configured_admin(db.conn, config.admin)
        assert second.created is False
        assert second.user_id == "admin"
        row = db.conn.execute(
            "SELECT password_hash FROM flr_sample_users WHERE user_id = 'admin'"
        ).fetchone()
        assert row[0] == original_hash


def test_admin_bootstrap_refuses_fresh_database_without_credentials(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path / "locked.yaml",
        "version: 1\ndatabase:\n  path: locked.db\n",
    )
    config = load_deployment_config(path)
    with MFDatabase(config.database.path) as db:
        with pytest.raises(ValueError, match="no active administrator"):
            ensure_configured_admin(db.conn, config.admin)


def test_admin_bootstrap_never_promotes_preexisting_non_service_identity(tmp_path: Path) -> None:
    db_path = tmp_path / "claimed.db"
    with MFDatabase(db_path) as db:
        db.add_user("claimed", display_name="Existing user", is_admin=0)
        config = AdminBootstrapConfig(
            user="claimed", password="Strong-local1!", allow_weak_bootstrap=False
        )
        with pytest.raises(ValueError, match="already exists"):
            ensure_configured_admin(db.conn, config)
        row = db.conn.execute(
            "SELECT is_admin, password_hash FROM flr_sample_users WHERE user_id = 'claimed'"
        ).fetchone()
        assert tuple(row) == (0, None)


def test_concurrent_first_admin_bootstrap_creates_exactly_once(tmp_path: Path) -> None:
    db_path = tmp_path / "concurrent.db"
    MFDatabase(db_path).close()
    bootstrap = AdminBootstrapConfig(
        user="admin", password="admin", allow_weak_bootstrap=True
    )
    barrier = threading.Barrier(2)
    outcomes = []
    errors = []

    def run() -> None:
        connection = sqlite3.connect(db_path, timeout=10)
        try:
            barrier.wait()
            outcomes.append(ensure_configured_admin(connection, bootstrap))
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert errors == []
    assert len(outcomes) == 2
    assert sorted(result.created for result in outcomes) == [False, True]
    with MFDatabase(db_path) as db:
        count = db.conn.execute(
            "SELECT COUNT(*) FROM flr_sample_users WHERE is_admin = 1 AND deleted_at IS NULL"
        ).fetchone()[0]
        assert count == 1


def test_cli_init_bootstraps_default_local_admin(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path / "compose.yaml",
        """
version: 1
database:
  path: mmfdb.db
admin:
  user: admin
  password: admin
  allow_weak_bootstrap: true
""",
    )
    runner = CliRunner()

    first = runner.invoke(cli, ["init", "--config", os.fspath(path), "--json"])
    assert first.exit_code == 0, first.output
    first_payload = json.loads(first.output)
    assert first_payload == {"ok": True, "user_id": "admin", "created": True}
    assert "password" not in first_payload

    second = runner.invoke(cli, ["init", "--config", os.fspath(path), "--json"])
    assert second.exit_code == 0, second.output
    assert '"created": false' in second.output.lower()


def test_compose_assets_are_local_only_and_use_healthcheck() -> None:
    package_root = Path(__file__).resolve().parents[1]
    compose = (package_root / "compose.yaml").read_text(encoding="utf-8")
    dockerfile = (package_root / "Dockerfile").read_text(encoding="utf-8")

    assert "127.0.0.1:8080:8080" in compose
    assert "/healthz" in compose
    assert "restart: unless-stopped" in compose
    assert 'CMD ["mmfdb", "serve"' in dockerfile
    assert "'.[postgres,ldap,s3]'" in dockerfile
    assert "python:3.12-slim@sha256:" in dockerfile
    assert "-c docker-constraints.txt" in dockerfile
    assert (package_root / "docker-constraints.txt").is_file()
    assert (package_root / "LICENSE").is_file()
    setup_hook = (package_root / "setup.py").read_text(encoding="utf-8")
    assert 'Path(self.build_lib) / "mfdb"' in setup_hook
