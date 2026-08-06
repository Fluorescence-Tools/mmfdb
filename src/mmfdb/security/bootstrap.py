"""Explicit local account bootstrap: the one-shot administrator, and seeded users.

Fresh databases are locked by default.  A deployment may opt into creating its
first local administrator by setting both bootstrap environment variables before
the database is created, or by registering credentials with
:func:`mmfdb.config.set_admin_bootstrap_resolver`.  It may likewise seed ordinary
working accounts through :func:`mmfdb.config.set_default_accounts_resolver`.  No
username or credential is supplied implicitly.
"""

from __future__ import annotations

import os
import sqlite3
import uuid
from dataclasses import dataclass

from mmfdb.config import AdminBootstrapConfig, LocalAccountConfig

BOOTSTRAP_USER_ENV = "MMFDB_BOOTSTRAP_ADMIN_USER"
BOOTSTRAP_PASSWORD_ENV = "MMFDB_BOOTSTRAP_ADMIN_PASSWORD"
_MAIN_BRANCH_UUID = "00000000-0000-0000-0000-000000000000"
SERVICE_USER_ID = "user_default"


@dataclass(frozen=True)
class AdminBootstrapResult:
    """Outcome of reconciling the one-shot configured administrator."""

    user_id: str
    created: bool


def ensure_locked_service_identity(
    conn: sqlite3.Connection, branch_uuid: str = _MAIN_BRANCH_UUID
) -> None:
    """Ensure repository-only attribution has a non-login foreign-key identity."""
    if conn.execute(
        "SELECT 1 FROM flr_sample_users WHERE user_id = ?", (SERVICE_USER_ID,)
    ).fetchone():
        return
    conn.execute(
        "INSERT INTO flr_sample_users "
        "(user_id, user_uuid, display_name, active_branch_uuid, is_admin, "
        " password_hash, allow_passwordless_login, auth_provider) "
        "VALUES (?, ?, 'Local service identity', ?, 0, NULL, 0, 'local')",
        (SERVICE_USER_ID, str(uuid.uuid4()), branch_uuid),
    )


def _promotable_identities() -> frozenset[str]:
    """Return the user ids MMFDB itself auto-creates for the acting identity.

    These are the only pre-existing rows the one-shot bootstrap may claim: no
    person registered them, so promoting one takes nothing from anyone.

    Returns
    -------
    frozenset of str
        The service identity and the configured default (acting) user id.

    """
    from mmfdb.config import configured_default_user_id

    return frozenset({SERVICE_USER_ID, configured_default_user_id()})


def bootstrap_local_admin_from_env(conn: sqlite3.Connection) -> str | None:
    """Create the first local admin only when explicit environment credentials exist.

    The operation is intentionally limited to databases with no active
    administrator. Bootstrap variables never overwrite an existing identity.
    """
    user_id = os.environ.get(BOOTSTRAP_USER_ENV)
    password = os.environ.get(BOOTSTRAP_PASSWORD_ENV)
    if user_id is None and password is None:
        return None
    if not user_id or not password:
        raise ValueError(
            f"Set both {BOOTSTRAP_USER_ENV} and {BOOTSTRAP_PASSWORD_ENV} to bootstrap MMFDB"
        )

    if conn.execute(
        "SELECT 1 FROM flr_sample_users WHERE is_admin = 1 AND deleted_at IS NULL LIMIT 1"
    ).fetchone():
        return None
    return bootstrap_local_admin(conn, user_id=user_id, password=password, commit=False)


def bootstrap_local_admin(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    password: str,
    commit: bool = True,
    enforce_password_strength: bool = True,
) -> str:
    """Create the first local administrator, refusing any existing admin."""
    user_id = user_id.strip()
    if not user_id or len(user_id) > 128 or any(ord(char) < 32 for char in user_id):
        raise ValueError(f"{BOOTSTRAP_USER_ENV} is invalid")

    from mmfdb.admin.backend.password_services import evaluate_password, hash_password

    strength = evaluate_password(password)
    if enforce_password_strength and strength["score"] < 4:
        raise ValueError(
            "Bootstrap administrator password is too weak. Requirements: "
            + ", ".join(strength["feedback"])
        )

    if conn.execute(
        "SELECT 1 FROM flr_sample_users WHERE is_admin = 1 AND deleted_at IS NULL LIMIT 1"
    ).fetchone():
        raise ValueError("MMFDB already has an active administrator")
    existing = conn.execute(
        "SELECT is_admin, password_hash, allow_passwordless_login "
        "FROM flr_sample_users WHERE user_id = ?",
        (user_id,),
    ).fetchone()

    row = conn.execute(
        "SELECT branch_uuid FROM mmfdb_branch WHERE name = 'main' AND deleted_at IS NULL"
    ).fetchone()
    branch_uuid = row[0] if row else _MAIN_BRANCH_UUID
    if row is None:
        conn.execute(
            "INSERT INTO mmfdb_branch (branch_uuid, name, description) VALUES (?, 'main', ?) ",
            (branch_uuid, "Default main branch"),
        )
    password_hash = hash_password(password)
    if existing:
        # Only an identity MMFDB auto-creates for the *acting* user may be
        # promoted: the service identity, or the configured
        # ``mmfdb.default_user_id`` that ordinary work stamps its writes with.
        # Claiming one of those takes nothing from anyone, because nobody
        # registered it — the deployment's own configuration did. Any other
        # existing name is refused: a configured secret must never be able to
        # take over an identity (test_a_placeholder_under_any_other_name_is_
        # still_refused), and that is a deliberate property, not an oversight.
        #
        # A promotable row must still look untouched. One that carries a
        # password, admin rights or passwordless login is somebody's account
        # even under a configured name, and is refused with the hint below,
        # because the collision is a setting rather than an attack.
        if user_id not in _promotable_identities() or existing[0] or existing[1] or existing[2]:
            hint = ""
            if not (existing[0] or existing[1] or existing[2]):
                from mmfdb.config import configured_default_user_id

                acting = configured_default_user_id()
                hint = (
                    f" — it exists as a plain user with no password, which is what "
                    f"an ordinary write creates for the acting identity. Only the "
                    f"acting identity may be claimed, and it is currently {acting!r}: "
                    f"point mmfdb.default_user_id at {user_id!r}, or bootstrap "
                    f"{acting!r} instead (the built-in acting identity is "
                    f"{SERVICE_USER_ID!r})"
                )
            raise ValueError(
                f"Bootstrap administrator {user_id!r} already exists{hint}"
            )
        conn.execute(
            "UPDATE flr_sample_users SET display_name = ?, active_branch_uuid = ?, "
            "is_admin = 1, password_hash = ?, allow_passwordless_login = 0 "
            "WHERE user_id = ?",
            (user_id, branch_uuid, password_hash, user_id),
        )
    else:
        conn.execute(
            "INSERT INTO flr_sample_users "
            "(user_id, user_uuid, display_name, active_branch_uuid, is_admin, "
            " password_hash, allow_passwordless_login, auth_provider) "
            "VALUES (?, ?, ?, ?, 1, ?, 0, 'local')",
            (user_id, str(uuid.uuid4()), user_id, branch_uuid, password_hash),
        )
    for group_id, role in (("users", "member"), ("admins", "admin")):
        if not conn.execute(
            "SELECT 1 FROM mmfdb_group WHERE group_id = ?", (group_id,)
        ).fetchone():
            continue
        if not conn.execute(
            "SELECT 1 FROM mmfdb_group_member WHERE group_id = ? AND user_id = ?",
            (group_id, user_id),
        ).fetchone():
            conn.execute(
                "INSERT INTO mmfdb_group_member (group_id, user_id, role) VALUES (?, ?, ?)",
                (group_id, user_id, role),
            )
    if commit:
        conn.commit()
    return user_id


def ensure_configured_admin(
    conn: sqlite3.Connection,
    config: AdminBootstrapConfig,
) -> AdminBootstrapResult:
    """Ensure a fresh deployment has one configured admin without resetting it.

    Once any active administrator exists, bootstrap credentials are ignored.
    This makes container restarts idempotent and prevents a YAML secret from
    becoming a perpetual password-reset mechanism.
    """
    owns_transaction = not conn.in_transaction
    if owns_transaction:
        # Serialize the no-admin check and creation across container/process
        # startups. A deferred transaction permits both contenders to observe
        # an empty result before either inserts.
        if isinstance(conn, sqlite3.Connection):
            conn.execute("BEGIN IMMEDIATE")
        else:
            conn.execute("BEGIN")
            conn.execute("LOCK TABLE flr_sample_users IN EXCLUSIVE MODE")
    try:
        existing = conn.execute(
            "SELECT user_id FROM flr_sample_users "
            "WHERE is_admin = 1 AND deleted_at IS NULL ORDER BY created_at LIMIT 1"
        ).fetchone()
        if existing:
            result = AdminBootstrapResult(user_id=str(existing[0]), created=False)
        else:
            if not config.user or config.password is None:
                raise ValueError(
                    "MMFDB has no active administrator; configure admin.user and admin.password "
                    "for the one-shot bootstrap"
                )
            created = bootstrap_local_admin(
                conn,
                user_id=config.user,
                password=config.password,
                commit=False,
                enforce_password_strength=not config.allow_weak_bootstrap,
            )
            result = AdminBootstrapResult(user_id=created, created=True)
        if owns_transaction:
            conn.commit()
        return result
    except Exception:
        if owns_transaction:
            conn.rollback()
        raise


def ensure_login_capable_admin(conn: sqlite3.Connection) -> AdminBootstrapResult | None:
    """Restore an administrator on a database that has none, when one is configured.

    A database that reaches this function with no active administrator cannot be
    logged into at all, so every path that creates or replaces a database (first
    start, reset from the curated seed) calls it. The credentials come from
    :func:`mmfdb.config.configured_admin_bootstrap`, i.e. a host resolver or the
    bootstrap environment variables; with neither configured the database stays
    locked, which is MMFDB's standalone default.

    Parameters
    ----------
    conn : sqlite3.Connection
        Open connection to the database to reconcile.

    Returns
    -------
    AdminBootstrapResult or None
        The reconciled administrator, or ``None`` when no bootstrap
        administrator is configured.

    """
    from mmfdb.config import configured_admin_bootstrap

    config = configured_admin_bootstrap()
    if config is None:
        return None
    return ensure_configured_admin(conn, config)


def ensure_local_account(conn: sqlite3.Connection, account: LocalAccountConfig) -> bool:
    """Make a configured ordinary account exist and be able to log in.

    An account someone actually holds — one with a password, admin rights or
    passwordless login — is left exactly as it is; a deployment secret must
    never overwrite a credential. What *is* filled in is the bare stub an
    ordinary write leaves behind when it stamps the acting identity: without
    this, a single write before the account is seeded would leave a permanently
    passwordless row nobody could sign in as.

    Parameters
    ----------
    conn : sqlite3.Connection
        Open connection to the database to seed.
    account : mmfdb.config.LocalAccountConfig
        The account to ensure. A password of ``None`` creates a login-incapable
        row unless ``allow_passwordless_login`` is set.

    Returns
    -------
    bool
        Whether the row was created or completed.

    """
    from mmfdb.admin.backend.password_services import hash_password

    user_id = account.user_id.strip()
    if not user_id or len(user_id) > 128 or any(ord(char) < 32 for char in user_id):
        raise ValueError(f"Configured account id {account.user_id!r} is invalid")
    existing = conn.execute(
        "SELECT is_admin, password_hash, allow_passwordless_login "
        "FROM flr_sample_users WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    if existing is not None:
        if existing[0] or existing[1] or existing[2]:
            return False
        conn.execute(
            "UPDATE flr_sample_users SET password_hash = ?, allow_passwordless_login = ? "
            "WHERE user_id = ?",
            (
                hash_password(account.password) if account.password else None,
                1 if account.allow_passwordless_login else 0,
                user_id,
            ),
        )
        return True

    row = conn.execute(
        "SELECT branch_uuid FROM mmfdb_branch WHERE name = 'main' AND deleted_at IS NULL"
    ).fetchone()
    branch_uuid = row[0] if row else _MAIN_BRANCH_UUID
    conn.execute(
        "INSERT INTO flr_sample_users "
        "(user_id, user_uuid, display_name, active_branch_uuid, is_admin, "
        " password_hash, allow_passwordless_login, auth_provider) "
        "VALUES (?, ?, ?, ?, 0, ?, ?, 'local')",
        (
            user_id,
            str(uuid.uuid4()),
            account.display_name or user_id,
            branch_uuid,
            hash_password(account.password) if account.password else None,
            1 if account.allow_passwordless_login else 0,
        ),
    )
    for group_id in account.groups:
        if not conn.execute(
            "SELECT 1 FROM mmfdb_group WHERE group_id = ?", (group_id,)
        ).fetchone():
            continue
        conn.execute(
            "INSERT INTO mmfdb_group_member (group_id, user_id, role) VALUES (?, ?, 'member')",
            (group_id, user_id),
        )
    return True


def ensure_default_accounts(conn: sqlite3.Connection) -> dict[str, object]:
    """Reconcile every account a host configured for a fresh or reset database.

    The administrator keeps its one-shot semantics (see
    :func:`ensure_login_capable_admin`); the ordinary accounts are created only
    when missing. Both are needed after a reset, because the curated seed ships
    no accounts and would otherwise leave a workspace nobody can enter.

    Parameters
    ----------
    conn : sqlite3.Connection
        Open connection to the database to reconcile.

    Returns
    -------
    dict
        ``admin_user`` (``None`` when unconfigured) and the list of ordinary
        ``accounts`` that now exist.

    """
    from mmfdb.config import configured_default_accounts

    admin = ensure_login_capable_admin(conn)
    accounts = configured_default_accounts()
    created_any = False
    for account in accounts:
        created_any |= ensure_local_account(conn, account)
    if created_any:
        conn.commit()
    return {
        "admin_user": admin.user_id if admin else None,
        "accounts": [account.user_id for account in accounts],
    }


def disable_legacy_builtin_credentials(conn: sqlite3.Connection) -> None:
    """Disable prerelease built-in credentials without touching customized admins."""
    from mmfdb.admin.backend.password_services import verify_password

    row = conn.execute(
        "SELECT password_hash FROM flr_sample_users WHERE user_id = 'user_default'"
    ).fetchone()
    if row and row[0] and verify_password("admin", row[0]):
        conn.execute(
            "UPDATE flr_sample_users SET is_admin = 0, password_hash = NULL, "
            "allow_passwordless_login = 0 WHERE user_id = 'user_default'"
        )
        conn.execute(
            "UPDATE mmfdb_session SET revoked_at = CURRENT_TIMESTAMP "
            "WHERE user_id = 'user_default' AND revoked_at IS NULL"
        )
    conn.execute(
        "UPDATE flr_sample_users SET allow_passwordless_login = 0, password_hash = NULL "
        "WHERE user_id = 'guest'"
    )
