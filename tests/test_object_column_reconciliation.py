"""A stamped-current database missing a canonical column is repaired on open.

Databases exist in the field carrying a v44/v45 stamp whose ``mmfdb_object``
never gained ``content_sha256``. Because migrations are version-gated, the
migration that introduced the column could never reach them, so *every* object
write failed with ``table mmfdb_object has no column named content_sha256``,
rolled back, and the caller only saw a logged RPC error.
"""

from __future__ import annotations

import pathlib
import sqlite3

from mmfdb.repository import MFDatabase
from mmfdb.schema.schema import (
    SCHEMA_VERSION,
    get_schema_version,
    migrate_schema,
    set_schema_version,
)


#: ``mmfdb_object`` as the affected databases actually carry it — the shape
#: before ``content_sha256`` was introduced.
_LEGACY_OBJECT_DDL = """CREATE TABLE mmfdb_object (
    object_uuid TEXT PRIMARY KEY,
    content_md5 TEXT NOT NULL UNIQUE,
    original_filename TEXT,
    size_bytes INTEGER,
    mime_type TEXT,
    storage_path TEXT NOT NULL,
    refcount INTEGER DEFAULT 1,
    metadata_json TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    created_by_user_uuid TEXT,
    storage_mode TEXT
)"""


def _downgrade_object_table(db_path: pathlib.Path) -> None:
    """Put back the pre-``content_sha256`` ``mmfdb_object``.

    ``ALTER TABLE ... DROP COLUMN`` refuses a UNIQUE column, so the table is
    rebuilt — which is also closer to how the affected databases got there.
    """
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("PRAGMA legacy_alter_table = ON")
    conn.execute("DROP INDEX IF EXISTS ix_mmfdb_object_sha256")
    conn.execute("ALTER TABLE mmfdb_object RENAME TO mmfdb_object_legacy")
    conn.execute(_LEGACY_OBJECT_DDL)
    conn.execute("DROP TABLE mmfdb_object_legacy")
    conn.commit()
    conn.close()


def test_missing_object_column_is_reconciled(tmp_path: pathlib.Path) -> None:
    db_path = tmp_path / "stamped_but_incomplete.db"
    with MFDatabase(db_path) as db:
        pass

    _downgrade_object_table(db_path)

    # Stamp it one version below current, as the shipped databases are: the
    # column is gone and no version-gated migration would put it back.
    conn = sqlite3.connect(db_path)
    set_schema_version(conn, SCHEMA_VERSION - 1)
    conn.commit()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(mmfdb_object)")}
    assert "content_sha256" not in cols
    conn.close()

    conn = sqlite3.connect(db_path)
    migrate_schema(conn)
    assert get_schema_version(conn) == SCHEMA_VERSION
    cols = {r[1] for r in conn.execute("PRAGMA table_info(mmfdb_object)")}
    assert "content_sha256" in cols
    conn.commit()
    conn.close()

    # ...and the write path that used to roll back now stores the row.
    with MFDatabase(db_path) as db:
        ref = db.put_object(data=b"payload", filename="probe.bin")
        row = db.conn.execute(
            "SELECT content_sha256 FROM mmfdb_object WHERE object_uuid = ?",
            (ref["object_uuid"],),
        ).fetchone()
        assert row[0] == ref["content_sha256"]
