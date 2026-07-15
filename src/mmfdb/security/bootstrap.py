"""Explicit, one-shot local administrator bootstrap.

Fresh databases are locked by default.  A deployment may opt into creating its
first local administrator by setting both bootstrap environment variables before
the database is created.  No username or credential is supplied implicitly.
"""

from __future__ import annotations

import os
import sqlite3
import uuid
from dataclasses import dataclass

from mmfdb.config import AdminBootstrapConfig

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
        if user_id != SERVICE_USER_ID or existing[0] or existing[1] or existing[2]:
            raise ValueError(f"Bootstrap administrator {user_id!r} already exists")
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
