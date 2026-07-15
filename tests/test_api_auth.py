"""api.py auth threading + enforcement (INC-04)."""

from __future__ import annotations

from pathlib import Path

import mmfdb.api as api
import pytest
from mmfdb.admin.backend.password_services import hash_password
from mmfdb.repository import MFDatabase
from mmfdb.security.auth import AuthError, PermissionDenied
from mmfdb.security.login import login


def _setup(tmp_path: Path, monkeypatch) -> Path:
    db_path = tmp_path / "api_auth.db"
    MFDatabase(db_path).close()
    # api.py binds resolve_database_path at import time.
    monkeypatch.setattr(api, "resolve_database_path", lambda *a, **k: db_path)
    monkeypatch.setattr(
        "mmfdb.store.database_resolver.resolve_database_path", lambda *a, **k: db_path
    )
    return db_path


def _user(db_path: Path, user_id: str, *, password: str = "pw", is_admin: int = 0) -> None:
    db = MFDatabase(db_path)
    db.conn.execute(
        "INSERT INTO flr_sample_users (user_id, display_name, is_admin, password_hash) "
        "VALUES (?, ?, ?, ?)",
        (user_id, user_id, is_admin, hash_password(password)),
    )
    db.conn.commit()
    db.close()


def _token(db_path: Path, user_id: str, password: str = "pw") -> dict:
    db = MFDatabase(db_path)
    try:
        result = login(db.conn, user_id=user_id, password=password)
    finally:
        db.close()
    return {"token": result["token"]}


def test_api_accepts_auth_and_stamps_owner_acl(tmp_path: Path, monkeypatch) -> None:
    db_path = _setup(tmp_path, monkeypatch)
    _user(db_path, "alice")
    _user(db_path, "bob")
    alice, bob = _token(db_path, "alice"), _token(db_path, "bob")

    # A write with a token succeeds and records an owner ACL (previously the v1
    # RPC methods TypeError'd because the function had no `auth` parameter).
    assert api.register_sample("s_alice", auth=alice)["ok"]

    db = MFDatabase(db_path)
    try:
        acl = db.conn.execute(
            "SELECT owner_user_id FROM mmfdb_object_acl WHERE object_type='sample' AND object_id='s_alice'"
        ).fetchone()
        assert acl is not None and acl["owner_user_id"] == "alice"
    finally:
        db.close()

    # The owner can read it; a different user cannot (ACL default is owner-only),
    # and it is filtered out of their listing.
    assert api.get_sample("s_alice", auth=alice)["sample"]["sample_id"] == "s_alice"
    with pytest.raises(PermissionDenied):
        api.get_sample("s_alice", auth=bob)
    ids = {s["sample_id"] for s in api.list_samples(auth=bob)["samples"]}
    assert "s_alice" not in ids
    ids_alice = {s["sample_id"] for s in api.list_samples(auth=alice)["samples"]}
    assert "s_alice" in ids_alice


def test_api_legacy_row_without_acl_is_admin_only(tmp_path: Path, monkeypatch) -> None:
    db_path = _setup(tmp_path, monkeypatch)
    _user(db_path, "carol")
    carol = _token(db_path, "carol")
    _user(db_path, "root", password="strong-pass", is_admin=1)
    admin = _token(db_path, "root", "strong-pass")
    # Missing authorization metadata never becomes an allow decision. Admins can
    # still inspect legacy rows to repair their ACLs.
    db = MFDatabase(db_path)
    db.add_sample("s_legacy", description="legacy")
    db.conn.commit()
    db.close()
    with pytest.raises(PermissionDenied):
        api.get_sample("s_legacy", auth=carol)
    assert api.get_sample("s_legacy", auth=admin)["sample"]["sample_id"] == "s_legacy"


def test_api_unauthenticated_write_fails_closed(tmp_path: Path, monkeypatch) -> None:
    db_path = _setup(tmp_path, monkeypatch)
    with pytest.raises(AuthError, match="Authentication required"):
        api.register_sample("s_inproc", auth=None)


def test_api_idor_blocked_for_non_admin(tmp_path: Path, monkeypatch) -> None:
    db_path = _setup(tmp_path, monkeypatch)
    _user(db_path, "dave")
    _user(db_path, "erin")
    dave = _token(db_path, "dave")
    # A non-admin cannot set another user's active branch.
    with pytest.raises(PermissionDenied):
        api.set_user_active_branch("erin", "00000000-0000-0000-0000-000000000000", auth=dave)
    # ...but may set their own.
    assert api.set_user_active_branch("dave", "00000000-0000-0000-0000-000000000000", auth=dave)["ok"]


def test_api_admin_may_target_other_user(tmp_path: Path, monkeypatch) -> None:
    db_path = _setup(tmp_path, monkeypatch)
    _user(db_path, "frank")
    _user(db_path, "root", password="strong-pass", is_admin=1)
    admin = _token(db_path, "root", "strong-pass")
    # An admin may read another user's active branch.
    result = api.get_user_active_branch("frank", auth=admin)
    assert "branch" in result
