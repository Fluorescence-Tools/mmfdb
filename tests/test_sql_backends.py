from __future__ import annotations

import pytest

from mmfdb import config
from mmfdb.repository import MFDatabase
from mmfdb.schema import schema
from mmfdb.store import database_resolver
from mmfdb.store.sql_backend import (
    DatabaseBackendError,
    DatabaseCapabilityError,
    DatabaseDriverError,
    DatabaseRow,
    PostgreSQLConnection,
    UnsupportedDatabaseBackend,
    _qmark_to_format,
    connect_postgresql,
    parse_database_target,
)


class _Cursor:
    def __init__(self, rows=(), columns=()) -> None:
        self._rows = list(rows)
        self.description = [(column,) for column in columns]
        self.rowcount = len(self._rows)
        self.lastrowid = None
        self.executed: list[tuple[str, tuple]] = []

    def execute(self, sql, parameters=()):
        self.executed.append((sql, tuple(parameters)))
        return self

    def fetchone(self):
        return self._rows.pop(0) if self._rows else None

    def fetchall(self):
        rows, self._rows = self._rows, []
        return rows

    def __iter__(self):
        return iter(self._rows)

    def close(self):
        pass


class _RawConnection:
    def __init__(self, cursor: _Cursor) -> None:
        self._cursor = cursor
        self.autocommit = False
        self.closed = False
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return self._cursor

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


class _Engine:
    def __init__(self, raw: _RawConnection) -> None:
        self.raw = raw
        self.disposed = False

    def raw_connection(self):
        return self.raw

    def dispose(self):
        self.disposed = True


class _ProvisionedPostgres:
    dialect = "postgresql"

    def __init__(self, version: int = schema.SCHEMA_VERSION) -> None:
        self.version = version
        self.in_transaction = False
        self.closed = False
        self.executed: list[tuple[str, tuple]] = []

    def execute(self, sql, parameters=()):
        self.executed.append((sql, tuple(parameters)))
        if "information_schema.tables" in sql:
            if "ORDER BY table_name" in sql:
                return _Rows([
                    DatabaseRow(["table_name"], [name])
                    for name in (
                        "flr_sample", "flr_sample_users", "mmfdb_artifact",
                        "mmfdb_operation", "mmfdb_object_acl", "mmfdb_audit_log",
                    )
                ])
            return _Rows([DatabaseRow(["table_name"], ["_schema_version"])])
        if 'SELECT version FROM "_schema_version"' in sql:
            return _Rows([DatabaseRow(["version"], [self.version])])
        if "information_schema.table_constraints" in sql:
            return _Rows([DatabaseRow(
                ["table_name", "column_name", "ordinal_position"],
                ["items", "item_id", 1],
            )])
        if "information_schema.columns" in sql:
            return _Rows([
                DatabaseRow(
                    [
                        "table_name", "column_name", "ordinal_position",
                        "data_type", "is_nullable", "column_default",
                    ],
                    ["items", "item_id", 1, "text", "NO", None],
                ),
                DatabaseRow(
                    [
                        "table_name", "column_name", "ordinal_position",
                        "data_type", "is_nullable", "column_default",
                    ],
                    ["items", "name", 2, "text", "YES", None],
                ),
            ])
        return _Rows([])

    def close(self):
        self.closed = True

    def commit(self):
        pass

    def rollback(self):
        pass


class _Rows:
    def __init__(self, rows) -> None:
        self.rows = list(rows)
        self.rowcount = len(self.rows)
        self.lastrowid = None

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)

    def __iter__(self):
        return iter(self.rows)


def test_database_targets_are_explicit_and_never_turn_urls_into_paths(tmp_path):
    sqlite = parse_database_target(tmp_path / "mmfdb.db")
    assert sqlite.dialect == "sqlite"
    assert sqlite.location == str(tmp_path / "mmfdb.db")

    postgres = parse_database_target(
        "postgresql://alice:secret@db.example/mmfdb?sslmode=require&sslpassword=hidden"
    )
    assert postgres.dialect == "postgresql"
    assert postgres.display_location == (
        "postgresql://alice:***@db.example/mmfdb?sslmode=require&sslpassword=%2A%2A%2A"
    )
    assert postgres.capabilities.schema_bootstrap is False

    assert parse_database_target("sqlite:///relative.db").location == "relative.db"
    assert parse_database_target("sqlite:////var/lib/mmfdb.db").location == (
        "/var/lib/mmfdb.db"
    )

    with pytest.raises(UnsupportedDatabaseBackend, match="Unsupported.*mysql"):
        parse_database_target("mysql://db.example/mmfdb")


def test_configured_database_url_has_precedence_and_is_not_created_as_file(tmp_path):
    config.configure_runtime(
        settings_dir=tmp_path,
        database_path=tmp_path / "local.db",
        database_url="postgresql://db.example/mmfdb",
    )
    try:
        assert database_resolver.resolve_database_location() == (
            "postgresql://db.example/mmfdb"
        )
        assert not (tmp_path / "local.db").exists()
    finally:
        config.reset_runtime_config()


def test_qmark_translation_ignores_literals_identifiers_and_comments():
    sql = (
        "SELECT '?', \"?\", value FROM t WHERE a = ? -- ?\n"
        "AND b = $$?$$ AND c = ? /* ? */"
    )
    assert _qmark_to_format(sql) == (
        "SELECT '?', \"?\", value FROM t WHERE a = %s -- ?\n"
        "AND b = $$?$$ AND c = %s /* ? */"
    )


def test_qmark_translation_escapes_literal_percent_for_psycopg_format_binding():
    assert _qmark_to_format(
        "SELECT '50%', value % 2 FROM items WHERE item_id = ?"
    ) == "SELECT '50%%', value %% 2 FROM items WHERE item_id = %s"


def test_postgresql_json_operator_is_not_misread_as_unbound_parameter():
    cursor = _Cursor(rows=[(True,)], columns=["present"])
    connection = PostgreSQLConnection(_RawConnection(cursor))
    connection.execute("SELECT payload ? 'camera' AS present")
    assert cursor.executed[-1][0] == "SELECT payload ? 'camera' AS present"
    with pytest.raises(DatabaseBackendError, match="placeholder count"):
        connection.execute(
            "SELECT payload ? 'camera' FROM items WHERE item_id = ?", ("item-1",)
        )


def test_postgresql_connection_context_is_atomic_and_nests_with_savepoints():
    cursor = _Cursor()
    raw = _RawConnection(cursor)
    connection = PostgreSQLConnection(raw)

    with connection:
        connection.execute("INSERT INTO items(item_id) VALUES (?)", ("one",))
        with connection:
            connection.execute("UPDATE items SET item_id = ?", ("two",))

    statements = [statement for statement, _ in cursor.executed]
    assert statements[0] == "BEGIN"
    assert statements[1] == "INSERT INTO items(item_id) VALUES (%s)"
    assert statements[2].startswith("SAVEPOINT mmfdb_ctx_")
    assert statements[3] == "UPDATE items SET item_id = %s"
    assert statements[4].startswith("RELEASE SAVEPOINT mmfdb_ctx_")
    assert raw.commits == 1


def test_postgresql_connection_context_rolls_back_on_error():
    cursor = _Cursor()
    raw = _RawConnection(cursor)
    connection = PostgreSQLConnection(raw)
    with pytest.raises(RuntimeError, match="stop"):
        with connection:
            raise RuntimeError("stop")
    assert raw.rollbacks == 1


def test_postgresql_adapter_translates_parameters_and_returns_mapping_rows():
    cursor = _Cursor(rows=[("item-1", "camera")], columns=["item_id", "name"])
    raw = _RawConnection(cursor)
    connection = PostgreSQLConnection(raw)

    row = connection.execute(
        "SELECT item_id, name FROM items WHERE item_id = ?", ("item-1",)
    ).fetchone()

    assert cursor.executed == [(
        "SELECT item_id, name FROM items WHERE item_id = %s", ("item-1",)
    )]
    assert row is not None
    assert row[0] == row["item_id"] == "item-1"
    assert dict(row) == {"item_id": "item-1", "name": "camera"}


def test_postgresql_adapter_rejects_sqlite_only_sql_before_driver_execution():
    cursor = _Cursor()
    connection = PostgreSQLConnection(_RawConnection(cursor))
    with pytest.raises(DatabaseCapabilityError, match="SQLite PRAGMA"):
        connection.execute("PRAGMA foreign_keys=ON")
    with pytest.raises(DatabaseCapabilityError, match="SQLite INSERT OR"):
        connection.execute("INSERT OR IGNORE INTO items(name) VALUES (?)", ("x",))
    assert cursor.executed == []


def test_repository_accepts_current_provisioned_postgresql_schema_and_introspects_dao():
    connection = _ProvisionedPostgres()
    db = MFDatabase(
        "postgresql://db.example/mmfdb",
        connection=connection,
        owns_connection=False,
    )
    try:
        assert db.database_target is not None
        assert db.database_target.dialect == "postgresql"
        assert db.db_path is None
        assert db.database_url == "postgresql://db.example/mmfdb"
        assert db.dao.primary_key("items") == "item_id"
        assert db.dao.columns("items") == {"item_id", "name"}
    finally:
        db.close()
    assert connection.closed is False


@pytest.mark.parametrize("version", [0, schema.SCHEMA_VERSION - 1])
def test_repository_fails_clearly_for_unprovisioned_or_old_postgresql_schema(version):
    connection = _ProvisionedPostgres(version)
    if version == 0:
        # No version table is represented by an empty information-schema result.
        original_execute = connection.execute

        def execute(sql, parameters=()):
            if "information_schema.tables" in sql and "ORDER BY table_name" not in sql:
                return _Rows([])
            return original_execute(sql, parameters)

        connection.execute = execute  # type: ignore[method-assign]
    with pytest.raises(DatabaseCapabilityError, match="schema|migrate"):
        MFDatabase(
            "postgresql://db.example/mmfdb",
            connection=connection,
            owns_connection=False,
        )


def test_server_database_backup_requires_native_tooling():
    db = MFDatabase(
        "postgresql://db.example/mmfdb",
        connection=_ProvisionedPostgres(),
        owns_connection=False,
    )
    with pytest.raises(DatabaseCapabilityError, match="native backup tooling"):
        db.backup_database("ignored.db")
    with pytest.raises(DatabaseCapabilityError, match="schema bootstrap"):
        db.create_tables()
    with pytest.raises(DatabaseCapabilityError, match="schema migration"):
        db.migrate()
    assert db.get_schema_version() == schema.SCHEMA_VERSION


def test_connect_postgresql_reports_missing_optional_driver(monkeypatch):
    sqlalchemy = pytest.importorskip("sqlalchemy")

    def fail(*args, **kwargs):
        raise ModuleNotFoundError("No module named 'psycopg'")

    monkeypatch.setattr(sqlalchemy, "create_engine", fail)
    target = parse_database_target("postgresql://db.example/mmfdb")
    with pytest.raises(DatabaseDriverError, match=r"mmfdb\[postgres\]"):
        connect_postgresql(target)


def test_connect_postgresql_uses_psycopg_url_and_owns_engine_lifecycle(monkeypatch):
    sqlalchemy = pytest.importorskip("sqlalchemy")

    cursor = _Cursor()
    raw = _RawConnection(cursor)
    engine = _Engine(raw)
    received = {}

    def create_engine(url, **options):
        received.update(url=url, options=options)
        return engine

    monkeypatch.setattr(sqlalchemy, "create_engine", create_engine)
    target = parse_database_target("postgresql://user:pw@db.example/mmfdb")
    connection = connect_postgresql(target, readonly=True)
    assert received == {
        "url": "postgresql+psycopg://user:pw@db.example/mmfdb",
        "options": {"future": True, "pool_pre_ping": True},
    }
    assert raw.autocommit is True
    assert cursor.executed == [("SET default_transaction_read_only = on", ())]
    connection.close()
    assert raw.closed is True
    assert engine.disposed is True
