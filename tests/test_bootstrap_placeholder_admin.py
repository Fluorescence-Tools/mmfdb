"""Which pre-existing row the one-shot administrator bootstrap may claim.

MMFDB stamps every write with ``mmfdb.default_user_id`` and auto-creates that
user as a plain row. The bootstrap refuses to claim a name that already exists,
deliberately, so a configured secret can never take over somebody's identity.

The exception is the identity MMFDB creates *itself* for the acting user — the
built-in service identity, or whatever ``mmfdb.default_user_id`` names. Nobody
registered those, so claiming one takes nothing from anyone, and a deployment
that legitimately runs as its own administrator (a single-user desktop) would
otherwise be locked out permanently by its own first write.

The exception is narrow: the row must still be a bare stub. A password, admin
rights or passwordless login make it an account, and it is refused with a
message naming the settings that collided — the bare "already exists" sent
readers looking for an attacker instead of a setting.
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


def test_the_configured_acting_identity_may_also_be_promoted(db):
    """A deployment whose acting identity *is* its administrator must still boot.

    Naming both ``admin`` is what a single-user desktop does. The row an
    ordinary write leaves behind under that name is created by the
    configuration, not by a person, so it is claimable exactly like the
    built-in service identity — otherwise the first write after a database
    reset permanently locks the deployment out.
    """
    from mmfdb.config import configure_runtime, reset_runtime_config

    _placeholder(db.conn, "admin")
    try:
        configure_runtime(default_user_id="admin")

        assert bootstrap_local_admin(db.conn, user_id="admin", password=PASSWORD) == "admin"
    finally:
        reset_runtime_config()

    is_admin, password_hash, passwordless = tuple(_row(db.conn, "admin"))
    assert (is_admin, bool(password_hash), passwordless) == (1, True, 0)


def test_a_configured_acting_identity_with_a_password_is_still_refused(db):
    """Configuration claims a stub, never an account somebody set a password on."""
    from mmfdb.config import configure_runtime, reset_runtime_config

    db.conn.execute(
        "INSERT INTO flr_sample_users (user_id, user_uuid, display_name, is_admin, "
        " password_hash, allow_passwordless_login) VALUES (?, ?, ?, 0, 'existing-hash', 0)",
        ("admin", str(uuid.uuid4()), "admin"),
    )
    try:
        configure_runtime(default_user_id="admin")

        with pytest.raises(ValueError, match="already exists"):
            bootstrap_local_admin(db.conn, user_id="admin", password=PASSWORD)
    finally:
        reset_runtime_config()

    assert _row(db.conn, "admin")[1] == "existing-hash", "the password is untouched"


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
