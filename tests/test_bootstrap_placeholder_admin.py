"""Naming the acting identity like the bootstrap admin is a configuration trap.

MMFDB stamps every write with ``mmfdb.default_user_id`` and auto-creates that
user as a plain row. The one-shot bootstrap then refuses to claim a name that
already exists — deliberately, so a configured secret can never take over an
existing identity (see
``test_admin_bootstrap_never_promotes_preexisting_non_service_identity``).

Configure both to the same name and those two rules meet: the first write claims
the name, and the deployment can never obtain an administrator. The refusal is
correct; what was missing is that it said nothing about *why* it was reached, so
the failure read as an attack rather than as a setting. These pin the refusal,
the one identity that may be promoted, and the message.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from mmfdb.repository import MFDatabase
from mmfdb.security.bootstrap import SERVICE_USER_ID, bootstrap_local_admin

PASSWORD = "Correct-Horse-Battery-Staple-9"


@pytest.fixture
def db(tmp_path: Path):
    database = MFDatabase(tmp_path / "bootstrap_test.db")
    yield database
    database.close()


def _placeholder(conn, user_id: str) -> None:
    """The row ordinary work leaves behind for the acting identity."""
    conn.execute(
        "INSERT INTO flr_sample_users (user_id, user_uuid, display_name, is_admin, "
        " password_hash, allow_passwordless_login) VALUES (?, ?, ?, 0, NULL, 0)",
        (user_id, str(uuid.uuid4()), user_id),
    )


def _row(conn, user_id: str):
    return conn.execute(
        "SELECT is_admin, password_hash, allow_passwordless_login "
        "FROM flr_sample_users WHERE user_id = ?",
        (user_id,),
    ).fetchone()


def test_the_service_identity_is_the_one_that_may_be_promoted(db):
    """It is seeded by MMFDB itself, so claiming it takes nothing from anyone."""
    assert _row(db.conn, SERVICE_USER_ID) is not None, "seeded by the database"

    created = bootstrap_local_admin(db.conn, user_id=SERVICE_USER_ID, password=PASSWORD)

    assert created == SERVICE_USER_ID
    is_admin, password_hash, passwordless = tuple(_row(db.conn, SERVICE_USER_ID))
    assert (is_admin, bool(password_hash), passwordless) == (1, True, 0)


def test_a_placeholder_under_any_other_name_is_still_refused(db):
    """The security property: a configured secret never claims an identity."""
    _placeholder(db.conn, "admin")

    with pytest.raises(ValueError, match="already exists"):
        bootstrap_local_admin(db.conn, user_id="admin", password=PASSWORD)

    assert tuple(_row(db.conn, "admin")) == (0, None, 0), "the row is untouched"


def test_the_refusal_names_the_configuration_that_caused_it(db):
    """Reached in practice by a setting, not by an attacker — say which.

    A bare "already exists" sent readers looking for a hostile row; the message
    now points at the two settings that collided.
    """
    _placeholder(db.conn, "admin")

    with pytest.raises(ValueError) as excinfo:
        bootstrap_local_admin(db.conn, user_id="admin", password=PASSWORD)

    message = str(excinfo.value)
    assert "mmfdb.default_user_id" in message
    assert SERVICE_USER_ID in message


def test_a_real_user_is_never_reset_by_the_bootstrap_secret(db):
    """A row with a password gets no hint and no promotion — just the refusal."""
    db.conn.execute(
        "INSERT INTO flr_sample_users (user_id, user_uuid, display_name, is_admin, "
        " password_hash, allow_passwordless_login) VALUES (?, ?, ?, 0, 'existing-hash', 0)",
        ("alice", str(uuid.uuid4()), "alice"),
    )

    with pytest.raises(ValueError) as excinfo:
        bootstrap_local_admin(db.conn, user_id="alice", password=PASSWORD)

    assert "mmfdb.default_user_id" not in str(excinfo.value), (
        "an existing account is not a configuration collision"
    )
    assert _row(db.conn, "alice")[1] == "existing-hash", "the password is untouched"


def test_registering_first_no_longer_claims_the_administrator_name(db):
    """The sequence that found this, with the identities kept apart.

    ``ensure_user`` is what every provenance write calls for the acting
    identity. With it defaulting to the service user, a registration before the
    first client start no longer stands in the bootstrap's way.
    """
    db.ensure_user(SERVICE_USER_ID)
    db.conn.commit()

    assert bootstrap_local_admin(db.conn, user_id="admin", password=PASSWORD) == "admin"
    assert _row(db.conn, "admin")[0] == 1
    assert _row(db.conn, SERVICE_USER_ID)[0] == 0, "the acting identity stays a plain user"
