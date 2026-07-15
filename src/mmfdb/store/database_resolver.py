"""Resolve the canonical MMFDB sample database path and object store root."""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

from mmfdb.config import (
    configured_database_path,
    configured_database_url,
    configured_object_store_root,
    configured_settings_dir,
    configured_source_database_path,
)

logger = logging.getLogger(__name__)

SOURCE_DB_NAME = "sample_management.db"
USER_DB_RELATIVE = Path("flr") / SOURCE_DB_NAME


def source_database_path() -> Path:
    """Return the curated source database shipped with MMFDB."""
    configured = configured_source_database_path()
    if configured is not None:
        return configured
    data_dir = Path(__file__).resolve().parent.parent / "data"
    packaged_source = data_dir / SOURCE_DB_NAME
    if packaged_source.exists():
        return packaged_source
    return data_dir / "example.db"


def user_database_path() -> Path:
    """Return the per-user editable sample database path."""
    configured = configured_database_path()
    if configured is not None:
        return configured
    return configured_settings_dir() / USER_DB_RELATIVE


def object_store_root() -> Path:
    """Return the shared object store root path.

    The object store is shared across all users on the same machine.
    By default it is located at ``{settings_dir}/objects/``. Configure
    ``mmfdb.store.object_store.root`` in ``settings_chisurf.yaml`` to override
    it. Relative configured paths are resolved relative to the settings
    directory.
    """
    settings_dir = configured_settings_dir()
    root = configured_object_store_root()
    if root is None:
        return settings_dir / "objects"
    root = Path(os.path.expandvars(os.path.expanduser(str(root))))
    if not root.is_absolute():
        root = settings_dir / root
    return root


def resolve_database_location() -> str | Path:
    """Return the configured SQL URL or ensure the local SQLite DB exists.

    ``MMFDB_DATABASE_URL`` has explicit precedence over file configuration.
    Server URLs are returned unchanged and are never passed through filesystem
    copy/bootstrap logic.
    """
    database_url = configured_database_url()
    if database_url is not None:
        from mmfdb.store.sql_backend import parse_database_target

        # Validate the scheme eagerly so a typo never becomes a SQLite path.
        parse_database_target(database_url)
        return database_url
    source_path = source_database_path()
    user_path = user_database_path()
    user_path.parent.mkdir(parents=True, exist_ok=True)

    if not user_path.exists():
        if source_path.exists():
            _copy_database(source_path, user_path)
        else:
            _create_empty_database(user_path)
    return user_path


def resolve_database_path() -> str | Path:
    """Compatibility alias for :func:`resolve_database_location`.

    The historical name is retained for plugin callers. New code should use
    ``resolve_database_location`` because the result may be a SQL URL.
    """
    return resolve_database_location()


def backup_database(db_path: str | Path) -> Path:
    """Create and integrity-check an online SQLite backup.

    Copying only the main file is unsafe in WAL mode because committed pages may
    still live in ``-wal``.  SQLite's backup API observes one consistent
    snapshot across both files.
    """
    from mmfdb.store.sql_backend import DatabaseCapabilityError, parse_database_target

    target = parse_database_target(db_path)
    if not target.capabilities.online_backup:
        raise DatabaseCapabilityError(
            f"MMFDB-managed online backup is not supported for {target.dialect}; "
            "use the database server's native backup tooling"
        )
    db_path = Path(target.location)
    if str(db_path) == ":memory:":
        raise ValueError("Cannot back up an in-memory database")
    backup_dir = db_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup_path = backup_dir / f"{db_path.name}.{stamp}.bak"
    _online_backup(db_path, backup_path)
    logger.info("Backed up sample database: %s", backup_path)
    return backup_path


def backup_database_before_migration(db_path: Path, target_version: int) -> Path | None:
    """Back up a database before schema migration if migration is needed."""
    db_path = Path(db_path)
    if str(db_path) == ":memory:":
        return None

    conn = sqlite3.connect(str(db_path))
    try:
        version = _read_schema_version(conn)
        if version is not None and version >= target_version:
            return None
    finally:
        conn.close()

    backup_dir = db_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup_path = backup_dir / f"{db_path.name}.{stamp}.bak"
    _online_backup(db_path, backup_path)
    logger.info("Backed up sample database before migration: %s", backup_path)
    return backup_path


def restore_database_backup(backup_path: Path, db_path: Path) -> None:
    """Atomically restore a closed database from a verified SQLite backup."""
    backup_path = Path(backup_path)
    db_path = Path(db_path)
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{db_path}{suffix}")
        if sidecar.exists():
            sidecar.unlink()
    _online_backup(backup_path, db_path)
    check = sqlite3.connect(str(db_path))
    try:
        result = check.execute("PRAGMA integrity_check").fetchone()
        if result is None or result[0] != "ok":
            raise RuntimeError(
                f"Restored SQLite database failed integrity check: {result!r}"
            )
    finally:
        check.close()


def _online_backup(source_path: Path, backup_path: Path) -> None:
    """Write *backup_path* through SQLite, then atomically publish it."""
    tmp_path = backup_path.with_suffix(backup_path.suffix + ".tmp")
    source = sqlite3.connect(str(source_path))
    destination = sqlite3.connect(str(tmp_path))
    try:
        source.backup(destination)
        destination.commit()
        result = destination.execute("PRAGMA integrity_check").fetchone()
        if result is None or result[0] != "ok":
            raise RuntimeError(f"SQLite backup integrity check failed: {result!r}")
    finally:
        destination.close()
        source.close()
    try:
        tmp_path.replace(backup_path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def _copy_database(source_path: Path, user_path: Path) -> None:
    tmp_path = user_path.with_suffix(user_path.suffix + ".tmp")
    try:
        shutil.copy2(source_path, tmp_path)
        tmp_path.replace(user_path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def _create_empty_database(path: Path) -> None:
    from mmfdb.schema import schema

    conn = sqlite3.connect(str(path))
    try:
        schema.migrate_schema(conn)
        conn.commit()
    finally:
        conn.close()


def _read_schema_version(conn: sqlite3.Connection) -> int | None:
    try:
        row = conn.execute("SELECT version FROM _schema_version").fetchone()
    except sqlite3.OperationalError:
        return None
    return int(row[0]) if row is not None else None


def _default_reference_spectra_path() -> Path:
    """Resolve the default fluorophore reference ``spectra.db`` path.

    Returns
    -------
    pathlib.Path
        Existing reference database path.
    """
    env_path = os.environ.get("MMFDB_REFERENCE_SPECTRA_DB")
    candidates: list[Path] = []
    if env_path:
        candidates.append(Path(env_path).expanduser())
    package_root = Path(__file__).resolve().parent.parent
    candidates.append(package_root / "data" / "spectra.db")
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise FileNotFoundError(
        "No default fluorophore reference spectra.db found; set MMFDB_REFERENCE_SPECTRA_DB"
    )
