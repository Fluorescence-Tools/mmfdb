"""Architecture guardrails for the standalone MMFDB package."""

from __future__ import annotations

import ast
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

PACKAGE_ROOT = Path(__file__).parents[1] / "src" / "mmfdb"


def test_classes_do_not_shadow_methods_in_the_same_body() -> None:
    """A class body must not silently replace an earlier method definition."""
    duplicates: list[str] = []
    for path in PACKAGE_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            definitions: dict[str, int] = {}
            for item in node.body:
                if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if item.name in definitions:
                    relative = path.relative_to(PACKAGE_ROOT)
                    duplicates.append(
                        f"{relative}:{item.lineno} {node.name}.{item.name} "
                        f"(first defined at line {definitions[item.name]})"
                    )
                definitions[item.name] = item.lineno

    assert duplicates == []


def test_standalone_package_does_not_import_chisurf() -> None:
    """MMFDB must remain importable without the ChiSurf host application."""
    violations: list[str] = []
    for path in PACKAGE_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            imported: list[str] = []
            if isinstance(node, ast.Import):
                imported = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported = [node.module]
            if any(name == "chisurf" or name.startswith("chisurf.") for name in imported):
                violations.append(f"{path.relative_to(PACKAGE_ROOT)}:{node.lineno}")

    assert violations == []


def test_readonly_repository_cannot_mutate_database(tmp_path: Path) -> None:
    """The readonly constructor must enforce its contract at SQLite level."""
    from mmfdb.repository import MFDatabase

    path = tmp_path / "readonly.db"
    with MFDatabase(path) as writable:
        writable.conn.execute("CREATE TABLE readonly_probe (value TEXT)")
        writable.conn.commit()

    with MFDatabase(path, readonly=True) as readonly:
        assert readonly.conn.execute("PRAGMA query_only").fetchone()[0] == 1
        try:
            readonly.conn.execute("INSERT INTO readonly_probe VALUES ('no')")
        except sqlite3.OperationalError as error:
            assert "readonly" in str(error).lower()
        else:
            raise AssertionError("readonly MFDatabase accepted a write")


def test_current_database_open_does_not_reconcile_schema(tmp_path: Path) -> None:
    """Schema reconciliation is a migration step, not a normal open side effect."""
    from mmfdb.repository import MFDatabase

    path = tmp_path / "current.db"
    MFDatabase(path).close()

    with patch("mmfdb.schema.schema.reconcile_current_schema") as reconcile:
        MFDatabase(path).close()

    reconcile.assert_not_called()


def test_injected_connection_is_borrowed_and_configured() -> None:
    """Attaching a connection must preserve ownership and repository invariants."""
    from mmfdb.repository import MFDatabase

    connection = sqlite3.connect(":memory:")
    with MFDatabase(connection=connection) as database:
        assert database.conn.row_factory is sqlite3.Row
        assert database.conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert database.get_schema_version() > 0

    # Leaving the repository context detaches but does not close caller state.
    assert connection.execute("SELECT 1").fetchone()[0] == 1
    connection.close()


def test_owned_injected_connection_is_closed() -> None:
    """Explicit ownership remains available for connection factories."""
    from mmfdb.repository import MFDatabase

    connection = sqlite3.connect(":memory:")
    with MFDatabase(connection=connection, owns_connection=True):
        pass

    try:
        connection.execute("SELECT 1")
    except sqlite3.ProgrammingError:
        pass
    else:
        raise AssertionError("owned injected connection remained open")


def test_future_schema_is_rejected_even_for_readonly_open(tmp_path: Path) -> None:
    """Older code must never write or read a database with unknown semantics."""
    from mmfdb.repository import MFDatabase
    from mmfdb.schema.schema import SCHEMA_VERSION

    path = tmp_path / "future.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE _schema_version (version INTEGER NOT NULL)")
    connection.execute("INSERT INTO _schema_version VALUES (?)", (SCHEMA_VERSION + 1,))
    connection.commit()
    connection.close()

    for readonly in (False, True):
        try:
            MFDatabase(path, readonly=readonly)
        except RuntimeError as error:
            assert "newer" in str(error).lower()
        else:
            raise AssertionError("future schema was accepted")


def test_populated_unversioned_database_requires_explicit_import(tmp_path: Path) -> None:
    """Version zero is fresh only when the file contains no application tables."""
    from mmfdb.repository import MFDatabase

    path = tmp_path / "unknown.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE scientific_data (value TEXT)")
    connection.execute("INSERT INTO scientific_data VALUES ('do not discard')")
    connection.commit()
    connection.close()

    try:
        MFDatabase(path)
    except RuntimeError as error:
        assert "unversioned" in str(error).lower()
    else:
        raise AssertionError("populated unversioned database was treated as fresh")


def test_failed_migration_restores_pre_migration_snapshot(
    tmp_path: Path, monkeypatch
) -> None:
    """Even a legacy helper commit cannot leave a half-migrated database."""
    from mmfdb.repository import MFDatabase
    from mmfdb.schema import schema

    path = tmp_path / "migration-failure.sqlite"
    with MFDatabase(path) as db:
        schema.set_schema_version(db.conn, schema.SCHEMA_VERSION - 1)
        db.conn.commit()

    original = schema.MIGRATIONS[schema.SCHEMA_VERSION]

    def fail_after_commit(conn):
        conn.execute("CREATE TABLE migration_partial (value TEXT)")
        conn.execute("INSERT INTO migration_partial VALUES ('partial')")
        conn.commit()
        raise RuntimeError("injected migration failure")

    monkeypatch.setitem(schema.MIGRATIONS, schema.SCHEMA_VERSION, fail_after_commit)
    with pytest.raises(RuntimeError, match="injected migration failure"):
        MFDatabase(path)
    monkeypatch.setitem(schema.MIGRATIONS, schema.SCHEMA_VERSION, original)

    connection = sqlite3.connect(path)
    try:
        assert schema.get_schema_version(connection) == schema.SCHEMA_VERSION - 1
        assert connection.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='migration_partial'"
        ).fetchone() is None
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        connection.close()
