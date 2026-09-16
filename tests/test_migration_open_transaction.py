"""Migrating a connection that has uncommitted writes must not hang.

``migrate_schema`` snapshots the database with ``sqlite3.Connection.backup``
before running the waterfall, so a failed migration can be rolled back. That call
retries a *busy* source forever — it loops on ``SQLITE_BUSY`` with a sleep and
takes no timeout — and a connection holding an open write transaction is busy
against itself. A caller that wrote to the connection and then asked to migrate
therefore hung rather than failing, with no diagnostic at all.

The symptom in practice: any application opening an old database inside its own
transaction stops responding on startup, and any test doing the same never
finishes.
"""

from __future__ import annotations

import sqlite3
import threading

import pytest

from mmfdb.schema import schema


def _connection() -> sqlite3.Connection:
    """An in-memory connection usable from the deadline worker thread."""
    return sqlite3.connect(":memory:", check_same_thread=False)


def _old_database(connection) -> None:
    """Mark *connection* as an old-schema database, leaving the write uncommitted."""
    connection.execute("CREATE TABLE IF NOT EXISTS mmfdb_schema_version (version INTEGER)")
    connection.execute("INSERT INTO mmfdb_schema_version (version) VALUES (1)")
    connection.execute("CREATE TABLE IF NOT EXISTS mmfdb_schema_version (version INTEGER)")
    connection.execute("INSERT INTO mmfdb_schema_version (version) VALUES (1)")


def _migrate_with_deadline(connection, seconds: float = 30.0):
    """Run the migration on a worker thread and fail if it outlives the deadline."""
    outcome: dict = {}

    def run():
        try:
            outcome["report"] = schema.migrate_schema(connection)
        except BaseException as exc:                     # noqa: BLE001 - reported below
            outcome["error"] = exc

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(seconds)
    if worker.is_alive():
        pytest.fail(
            f"migrate_schema did not finish within {seconds:g} s — the snapshot "
            f"backup is retrying a busy source forever"
        )
    if "error" in outcome:
        raise outcome["error"]
    return outcome.get("report")


def test_migration_completes_with_an_open_transaction():
    """The waterfall runs even though the caller left a write uncommitted."""
    connection = _connection()
    _old_database(connection)
    assert connection.in_transaction, "the fixture must leave a write pending"

    report = _migrate_with_deadline(connection)

    assert report is not None
    assert report.from_version == 1
    assert report.to_version == schema.SCHEMA_VERSION
    assert schema.get_schema_version(connection) == schema.SCHEMA_VERSION
    connection.close()


def test_pending_writes_survive_the_migration():
    """Committing to take the snapshot must not discard what the caller wrote."""
    connection = _connection()
    _old_database(connection)
    connection.execute("CREATE TABLE IF NOT EXISTS caller_data (value TEXT)")
    connection.execute("INSERT INTO caller_data (value) VALUES ('written before migrating')")
    assert connection.in_transaction

    _migrate_with_deadline(connection)

    rows = connection.execute("SELECT value FROM caller_data").fetchall()
    assert rows == [("written before migrating",)]
    connection.close()


def test_already_committed_connection_still_migrates():
    """The ordinary case — nothing pending — is unchanged."""
    connection = _connection()
    _old_database(connection)
    connection.commit()
    assert not connection.in_transaction

    report = _migrate_with_deadline(connection)

    assert report is not None and report.from_version == 1
    assert schema.get_schema_version(connection) == schema.SCHEMA_VERSION
    connection.close()
