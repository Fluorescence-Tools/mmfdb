"""Focused regression tests for MMFDB API request resource ownership."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import mmfdb.api as api
import mmfdb.request_context as request_context
from mmfdb.admin.backend import services
from mmfdb.admin.backend.password_services import hash_password
from mmfdb.repository import MFDatabase
from mmfdb.security.login import login
from mmfdb.security.auth import create_default_acl_for_object
from mmfdb.security.auth import AuthError


class _Dispatcher:
    def __init__(self) -> None:
        self.handlers: dict[str, Any] = {}

    def register(self, name: str, handler: Any) -> None:
        self.handlers[name] = handler


def _database_with_token(path: Path) -> dict[str, str]:
    with MFDatabase(path) as database:
        database.conn.execute(
            "INSERT INTO flr_sample_users "
            "(user_id, display_name, is_admin, password_hash) VALUES (?, ?, ?, ?)",
            ("alice", "Alice", 0, hash_password("pw")),
        )
        database.add_sample("sample-1", description="context test")
        create_default_acl_for_object(database.conn, "sample", "sample-1", "alice")
        database.conn.commit()
        token = login(database.conn, user_id="alice", password="pw")["token"]
    return {"token": token}


def _count_request_resources(
    monkeypatch,
) -> tuple[list[Path], list[object], list[object]]:
    opened: list[Path] = []
    closed: list[object] = []
    authenticated: list[object] = []
    real_database = request_context.MFDatabase
    real_authenticate = request_context.principal_from_rpc_auth

    class CountingDatabase(real_database):
        def __init__(self, path, *args, **kwargs):
            opened.append(Path(path))
            super().__init__(path, *args, **kwargs)

        def close(self):
            closed.append(self)
            super().close()

    def counting_authenticate(conn, auth):
        authenticated.append(conn)
        return real_authenticate(conn, auth)

    monkeypatch.setattr(request_context, "MFDatabase", CountingDatabase)
    monkeypatch.setattr(request_context, "principal_from_rpc_auth", counting_authenticate)
    return opened, closed, authenticated


def test_v1_dispatch_uses_one_context_and_preserves_params(tmp_path, monkeypatch) -> None:
    path = tmp_path / "dispatcher.db"
    auth = _database_with_token(path)
    monkeypatch.setattr(
        "mmfdb.store.database_resolver.resolve_database_path", lambda *a, **k: path
    )
    opened, closed, authenticated = _count_request_resources(monkeypatch)

    dispatcher = _Dispatcher()
    services.register_services(dispatcher)
    params = {"sample_id": "sample-1", "auth": auth}
    original = {"sample_id": "sample-1", "auth": dict(auth)}

    result = dispatcher.handlers["mmfdb.v1.samples.get"](params)

    assert result["sample"]["sample_id"] == "sample-1"
    assert params == original
    assert len(opened) == 1
    assert len(closed) == 1
    assert len(authenticated) == 1


def test_direct_api_call_owns_one_context(tmp_path, monkeypatch) -> None:
    path = tmp_path / "direct.db"
    auth = _database_with_token(path)
    monkeypatch.setattr(api, "resolve_database_path", lambda *a, **k: path)
    opened, closed, authenticated = _count_request_resources(monkeypatch)

    result = api.get_sample("sample-1", auth=auth)

    assert result["sample"]["sample_id"] == "sample-1"
    assert len(opened) == 1
    assert len(closed) == 1
    assert len(authenticated) == 1


def test_legacy_dispatch_handlers_are_authenticated_without_auth_kwarg(
    tmp_path, monkeypatch
) -> None:
    """The dispatcher secures legacy signatures without injecting unknown kwargs."""
    path = tmp_path / "legacy-handler.db"
    auth = _database_with_token(path)
    monkeypatch.setattr(services, "resolve_database_path", lambda *a, **k: path)
    dispatcher = _Dispatcher()
    services.register_services(dispatcher)

    with pytest.raises(AuthError, match="Authentication required"):
        dispatcher.handlers["mmfdb.probes.list"]({})

    assert dispatcher.handlers["mmfdb.probes.list"]({"auth": auth}) == {"probes": []}
