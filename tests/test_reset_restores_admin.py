"""Resetting the user database must not leave a workspace nobody can enter.

The curated seed ships no accounts, so replacing the user database with it wipes
every administrator and every working identity. A host that configured them gets
them restored; a standalone deployment that configured none stays locked, which
is MMFDB's default.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from mmfdb.config import (
    AdminBootstrapConfig,
    LocalAccountConfig,
    configure_runtime,
    reset_runtime_config,
    set_admin_bootstrap_resolver,
    set_default_accounts_resolver,
)
from mmfdb.repository import MFDatabase
from mmfdb.security.bootstrap import bootstrap_local_admin
from mmfdb.security.login import login
from mmfdb.store.database_resolver import reset_user_database_from_source


@pytest.fixture
def workspace(tmp_path: Path):
    """Yield an isolated user/source database pair with a seed carrying no admin."""
    source = tmp_path / "seed" / "sample_management.db"
    user = tmp_path / "flr" / "sample_management.db"
    source.parent.mkdir(parents=True)
    user.parent.mkdir(parents=True)
    MFDatabase(source).close()
    configure_runtime(database_path=user, source_database_path=source)
    try:
        yield user
    finally:
        reset_runtime_config()


def _admins(db_path: Path) -> set[str]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return {
            row[0]
            for row in conn.execute(
                "SELECT user_id FROM flr_sample_users "
                "WHERE is_admin = 1 AND deleted_at IS NULL"
            )
        }
    finally:
        conn.close()


def test_reset_restores_the_configured_administrator(workspace: Path) -> None:
    set_admin_bootstrap_resolver(
        lambda: AdminBootstrapConfig(user="admin", password="admin", allow_weak_bootstrap=True)
    )
    with MFDatabase(workspace) as db:
        bootstrap_local_admin(db.conn, user_id="admin", password="admin",
                              enforce_password_strength=False)
        db.add_user("transient", "Created after the seed")

    result = reset_user_database_from_source()

    assert result["ok"] is True
    assert result["admin_user"] == "admin"
    assert Path(str(result["backup_path"])).exists()
    assert _admins(workspace) == {"admin"}
    with MFDatabase(workspace) as db:
        authenticated = login(db.conn, user_id="admin", password="admin")
        users = {row[0] for row in db.conn.execute("SELECT user_id FROM flr_sample_users")}
    assert authenticated["authenticated"] is True
    assert "transient" not in users


def test_reset_restores_the_configured_working_accounts(workspace: Path) -> None:
    """An unprivileged account is what a desktop actually works as."""
    set_admin_bootstrap_resolver(
        lambda: AdminBootstrapConfig(user="admin", password="admin", allow_weak_bootstrap=True)
    )
    set_default_accounts_resolver(
        lambda: [LocalAccountConfig(user_id="user", password="user", display_name="Local user")]
    )

    result = reset_user_database_from_source()

    assert result["accounts"] == ["user"]
    with MFDatabase(workspace) as db:
        authenticated = login(db.conn, user_id="user", password="user")
    assert authenticated["authenticated"] is True
    assert authenticated["user"]["is_admin"] is False
    assert _admins(workspace) == {"admin"}, "the working account gains no privileges"


def test_a_seeded_account_never_overwrites_a_credential(workspace: Path) -> None:
    """A deployment secret completes a stub; it never resets a real account."""
    from mmfdb.security.bootstrap import ensure_default_accounts

    set_admin_bootstrap_resolver(None)
    set_default_accounts_resolver(
        lambda: [LocalAccountConfig(user_id="user", password="seeded-password")]
    )
    reset_user_database_from_source()
    with MFDatabase(workspace) as db, db.conn:
        db.conn.execute(
            "UPDATE flr_sample_users SET password_hash = 'chosen-by-the-user' "
            "WHERE user_id = 'user'"
        )

    with MFDatabase(workspace) as db:
        ensure_default_accounts(db.conn)
        password_hash = db.conn.execute(
            "SELECT password_hash FROM flr_sample_users WHERE user_id = 'user'"
        ).fetchone()[0]

    assert password_hash == "chosen-by-the-user"


def test_a_stub_left_by_an_ordinary_write_is_completed(workspace: Path) -> None:
    """Stamping a write auto-creates a passwordless row — it must stay loginable.

    Without this the working account is unusable the moment anything is written
    before it is seeded, and nothing about the row says why.
    """
    from mmfdb.security.bootstrap import ensure_default_accounts

    set_admin_bootstrap_resolver(None)
    set_default_accounts_resolver(
        lambda: [LocalAccountConfig(user_id="user", password="user")]
    )
    with MFDatabase(workspace) as db, db.conn:
        db.ensure_user("user")

    with MFDatabase(workspace) as db:
        ensure_default_accounts(db.conn)
        authenticated = login(db.conn, user_id="user", password="user")

    assert authenticated["authenticated"] is True
    assert authenticated["user"]["is_admin"] is False


def test_reset_without_configured_credentials_stays_locked(workspace: Path) -> None:
    set_admin_bootstrap_resolver(None)
    set_default_accounts_resolver(None)
    with MFDatabase(workspace) as db:
        bootstrap_local_admin(db.conn, user_id="admin", password="Site-pass1!")

    result = reset_user_database_from_source()

    assert result["admin_user"] is None
    assert _admins(workspace) == set()


def test_reset_discards_the_replaced_databases_sidecars(workspace: Path) -> None:
    """A leftover ``-wal`` describes pages of a file the reset just replaced.

    Left in place beside the seed, SQLite replays it into a database it was never
    written for. Sidecars survive an unclean shutdown, which is exactly when a
    user reaches for a reset, so the reset must clear them.
    """
    set_admin_bootstrap_resolver(None)
    set_default_accounts_resolver(None)
    sidecars = [Path(f"{workspace}-wal"), Path(f"{workspace}-shm")]
    for sidecar in sidecars:
        sidecar.write_bytes(b"leftover from the replaced database")

    reset_user_database_from_source()

    assert [sidecar for sidecar in sidecars if sidecar.exists()] == []
