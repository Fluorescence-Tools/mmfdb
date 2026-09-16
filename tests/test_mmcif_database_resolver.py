"""Tests for fluorescence sample database path resolution and backup."""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

from mmfdb.store import database_resolver
from mmfdb.schema import schema
from mmfdb.store.database_resolver import backup_database_before_migration


def test_backup_before_migration_copies_existing_database():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "sample_management.db"
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE mmfdb_schema_version (version INTEGER)")
        conn.execute("INSERT INTO mmfdb_schema_version VALUES (8)")
        conn.commit()
        conn.close()

        backup_path = backup_database_before_migration(db_path, schema.SCHEMA_VERSION)

        assert backup_path is not None
        assert backup_path.exists()
        backup = sqlite3.connect(backup_path)
        try:
            assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert backup.execute("SELECT version FROM mmfdb_schema_version").fetchone()[0] == 8
        finally:
            backup.close()


def test_backup_includes_committed_wal_rows(tmp_path):
    """Online backup is the only safe copy primitive for a live WAL database."""
    db_path = tmp_path / "wal.db"
    writer = sqlite3.connect(db_path)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("CREATE TABLE payload (value TEXT)")
    writer.execute("INSERT INTO payload VALUES ('committed-in-wal')")
    writer.commit()
    try:
        backup_path = database_resolver.backup_database(db_path)
    finally:
        writer.close()

    backup = sqlite3.connect(backup_path)
    try:
        assert backup.execute("SELECT value FROM payload").fetchone()[0] == "committed-in-wal"
        assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        backup.close()


def test_copy_source_to_user_path(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        source = root / "src"
        user_root = root / "user"
        source.mkdir()
        user_root.mkdir()
        source_db = source / "sample_management.db"
        user_db = user_root / "flr" / "sample_management.db"
        # A real database, because installing the seed migrates the copy.
        seed_conn = sqlite3.connect(source_db)
        try:
            schema.migrate_schema(seed_conn)
            seed_conn.execute("CREATE TABLE curated (value TEXT)")
            seed_conn.execute("INSERT INTO curated VALUES ('curated-db')")
            seed_conn.commit()
        finally:
            seed_conn.close()

        monkeypatch.setattr(database_resolver, "source_database_path", lambda: source_db)
        monkeypatch.setattr(database_resolver, "user_database_path", lambda: user_db)

        resolved = database_resolver.resolve_database_path()

        assert resolved == user_db
        copied = sqlite3.connect(f"file:{user_db}?mode=ro", uri=True)
        try:
            assert copied.execute("SELECT value FROM curated").fetchone()[0] == "curated-db"
        finally:
            copied.close()


def test_packaged_seed_resolves_when_nothing_is_configured(monkeypatch):
    """The shipped seed is what an unconfigured install copies on first run."""
    monkeypatch.setattr(
        database_resolver, "configured_source_database_path", lambda: None
    )

    resolved = database_resolver.source_database_path()

    data_dir = Path(database_resolver.__file__).resolve().parent.parent / "data"
    assert resolved == data_dir / database_resolver.SOURCE_DB_NAME
    assert resolved.exists(), "the curated seed is package data and must ship"
    assert resolved.stat().st_size > 0


def test_packaged_seed_is_curated_and_openable():
    """The seed carries the curated reference content and is self-consistent.

    Its schema version is deliberately not pinned to the current one: the seed
    is package data written once, and the install migrates the copy it makes
    (see the migration test below).
    """
    data_dir = Path(database_resolver.__file__).resolve().parent.parent / "data"
    seed = data_dir / database_resolver.SOURCE_DB_NAME

    conn = sqlite3.connect(f"file:{seed}?mode=ro", uri=True)
    try:
        version = conn.execute("SELECT version FROM mmfdb_schema_version").fetchone()[0]
        assert 0 < version <= schema.SCHEMA_VERSION, (
            f"seed schema {version} is newer than this MMFDB's {schema.SCHEMA_VERSION}"
        )
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        for table, minimum in (
            ("probes", 7),
            ("spectra", 14),
            ("optical_properties", 27),
            ("flr_sample", 3),
            ("flr_experiment", 4),
        ):
            count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            assert count >= minimum, f"{table}: {count} < {minimum}"
    finally:
        conn.close()


def test_a_seed_behind_the_schema_is_migrated_when_it_is_installed(monkeypatch, tmp_path):
    """A seed written for an older schema still yields a current database.

    The seed is package data: it is written once and every release after that
    leaves it behind. The copy made on first run is migrated, so the version it
    was written at never reaches the user.
    """
    seed = tmp_path / "seed" / "sample_management.db"
    seed.parent.mkdir()
    user_db = tmp_path / "user" / "flr" / "sample_management.db"

    conn = sqlite3.connect(seed)
    try:
        schema.migrate_schema(conn)
        conn.commit()
        # Wind the stamp back to a version this MMFDB has a migration for, as a
        # seed shipped by an earlier release would be.
        older = max(v for v in schema.MIGRATIONS if v <= schema.SCHEMA_VERSION)
        schema.set_schema_version(conn, older - 1)
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(database_resolver, "configured_database_url", lambda: None)
    monkeypatch.setattr(database_resolver, "source_database_path", lambda: seed)
    monkeypatch.setattr(database_resolver, "user_database_path", lambda: user_db)

    resolved = Path(database_resolver.resolve_database_location())

    assert resolved == user_db
    conn = sqlite3.connect(f"file:{resolved}?mode=ro", uri=True)
    try:
        assert schema.get_schema_version(conn) == schema.SCHEMA_VERSION
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()
    # The seed itself is package data and is never written to.
    conn = sqlite3.connect(f"file:{seed}?mode=ro", uri=True)
    try:
        assert schema.get_schema_version(conn) == older - 1
    finally:
        conn.close()


def test_no_placeholder_database_shadows_a_missing_seed():
    """The 0-byte ``example.db`` fallback masked a missing seed (INC-15)."""
    data_dir = Path(database_resolver.__file__).resolve().parent.parent / "data"
    assert not (data_dir / "example.db").exists()


def test_missing_seed_yields_an_empty_but_migrated_database(monkeypatch, tmp_path):
    """Without a seed the user database is still a valid current-schema one."""
    user_db = tmp_path / "flr" / "sample_management.db"
    monkeypatch.setattr(
        database_resolver, "source_database_path", lambda: tmp_path / "absent.db"
    )
    monkeypatch.setattr(database_resolver, "user_database_path", lambda: user_db)

    resolved = database_resolver.resolve_database_location()

    assert resolved == user_db
    conn = sqlite3.connect(user_db)
    try:
        assert conn.execute("SELECT version FROM mmfdb_schema_version").fetchone()[0] == (
            schema.SCHEMA_VERSION
        )
    finally:
        conn.close()
