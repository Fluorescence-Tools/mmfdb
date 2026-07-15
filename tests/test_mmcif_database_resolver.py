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
        conn.execute("CREATE TABLE _schema_version (version INTEGER)")
        conn.execute("INSERT INTO _schema_version VALUES (8)")
        conn.commit()
        conn.close()

        backup_path = backup_database_before_migration(db_path, schema.SCHEMA_VERSION)

        assert backup_path is not None
        assert backup_path.exists()
        backup = sqlite3.connect(backup_path)
        try:
            assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert backup.execute("SELECT version FROM _schema_version").fetchone()[0] == 8
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
        source_db.write_bytes(b"curated-db")

        monkeypatch.setattr(database_resolver, "source_database_path", lambda: source_db)
        monkeypatch.setattr(database_resolver, "user_database_path", lambda: user_db)

        resolved = database_resolver.resolve_database_path()

        assert resolved == user_db
        assert user_db.read_bytes() == b"curated-db"
