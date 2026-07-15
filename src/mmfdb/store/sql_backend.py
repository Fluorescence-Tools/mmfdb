"""Database target parsing and DB-API compatibility for MMFDB SQL backends.

MMFDB's historical persistence implementation is SQLite-first.  This module is
the explicit boundary between that implementation and server SQL databases.  It
does not claim that SQLite DDL or administrative maintenance SQL is portable:
backend capabilities are declared and unsupported operations fail before any
destructive statement is attempted.

The PostgreSQL adapter deliberately exposes the small connection protocol used
by the repository (``execute``, cursors, mapping rows, commit/rollback and
savepoints).  Existing parameterised repository SQL uses PEP-249 ``qmark``
placeholders; the adapter translates those placeholders to the driver's native
format while respecting quoted SQL text and comments.
"""

from __future__ import annotations

import re
import sqlite3
import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast, overload
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit


class DatabaseBackendError(RuntimeError):
    """Base error for database target and backend failures."""


class UnsupportedDatabaseBackend(DatabaseBackendError):
    """Raised when a database URL selects an unknown backend."""


class DatabaseCapabilityError(DatabaseBackendError):
    """Raised when an operation is not implemented safely by a backend."""


class DatabaseDriverError(DatabaseBackendError):
    """Raised when an optional server-database driver cannot be loaded."""


@dataclass(frozen=True, slots=True)
class BackendCapabilities:
    """Durability features implemented by a database backend."""

    schema_bootstrap: bool
    schema_migrations: bool
    online_backup: bool
    readonly_connections: bool


SQLITE_CAPABILITIES = BackendCapabilities(
    schema_bootstrap=True,
    schema_migrations=True,
    online_backup=True,
    readonly_connections=True,
)

POSTGRESQL_CAPABILITIES = BackendCapabilities(
    # PostgreSQL is a supported runtime target for an already provisioned
    # current MMFDB schema.  Schema creation/migration remains a separate,
    # explicit deployment step until native PostgreSQL migrations exist.
    schema_bootstrap=False,
    schema_migrations=False,
    online_backup=False,
    readonly_connections=True,
)


@dataclass(frozen=True, slots=True)
class DatabaseTarget:
    """Parsed database location with a normalized dialect and capabilities."""

    dialect: str
    location: str
    capabilities: BackendCapabilities

    @property
    def is_sqlite(self) -> bool:
        return self.dialect == "sqlite"

    @property
    def display_location(self) -> str:
        """Return a password-redacted location suitable for logs/errors."""
        if self.is_sqlite:
            return self.location
        parsed = urlsplit(self.location)
        host = parsed.hostname or ""
        if parsed.port is not None:
            host += f":{parsed.port}"
        if parsed.username:
            host = f"{parsed.username}:***@{host}"
        sensitive = ("pass", "password", "secret", "token", "key")
        query = urlencode(
            [
                (key, "***" if any(word in key.lower() for word in sensitive) else value)
                for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            ]
        )
        return urlunsplit((parsed.scheme, host, parsed.path, query, ""))


def parse_database_target(location: str | Path) -> DatabaseTarget:
    """Parse a filesystem path or SQL URL without silently changing dialects."""
    raw = str(location)
    if raw == ":memory:" or "://" not in raw:
        return DatabaseTarget("sqlite", raw, SQLITE_CAPABILITIES)

    parsed = urlsplit(raw)
    scheme = parsed.scheme.lower()
    base_scheme = scheme.split("+", 1)[0]
    if base_scheme == "sqlite":
        if parsed.netloc not in ("", "localhost"):
            raise UnsupportedDatabaseBackend(
                "SQLite URLs cannot name a remote host; use sqlite:///absolute/path.db"
            )
        if parsed.query or parsed.fragment:
            raise DatabaseBackendError(
                "SQLite URL query/fragment options are not supported; configure "
                "read-only mode through MFDatabase(readonly=True)"
            )
        if parsed.path in ("/:memory:", ":memory:"):
            path = ":memory:"
        elif raw.startswith("sqlite:////"):
            path = "/" + unquote(raw[len("sqlite:////") :])
        elif raw.startswith("sqlite:///"):
            path = unquote(raw[len("sqlite:///") :])
        else:
            raise DatabaseBackendError(
                "SQLite URLs must use sqlite:///relative.db or sqlite:////absolute/path.db"
            )
        return DatabaseTarget("sqlite", path, SQLITE_CAPABILITIES)
    if base_scheme in {"postgres", "postgresql"}:
        if not parsed.hostname:
            raise DatabaseBackendError("PostgreSQL URL must include a hostname")
        if not parsed.path or parsed.path == "/":
            raise DatabaseBackendError("PostgreSQL URL must include a database name")
        return DatabaseTarget("postgresql", raw, POSTGRESQL_CAPABILITIES)
    raise UnsupportedDatabaseBackend(
        f"Unsupported MMFDB database URL scheme {parsed.scheme!r}; "
        "supported schemes are sqlite and postgresql"
    )


class DatabaseRow(Sequence[Any]):
    """Tuple-compatible row with sqlite3.Row-style name lookup."""

    __slots__ = ("_columns", "_index", "_values")

    def __init__(self, columns: Sequence[str], values: Sequence[Any]) -> None:
        self._columns = tuple(columns)
        self._index = {name: index for index, name in enumerate(self._columns)}
        self._values = tuple(values)

    def keys(self) -> list[str]:
        return list(self._columns)

    def __getitem__(self, key: int | slice | str) -> Any:
        if isinstance(key, str):
            return self._values[self._index[key]]
        return self._values[key]

    def __len__(self) -> int:
        return len(self._values)

    def __iter__(self) -> Iterator[Any]:
        return iter(self._values)


class ServerCursor:
    """Cursor wrapper returning :class:`DatabaseRow` values."""

    def __init__(self, cursor: Any) -> None:
        self._cursor = cursor

    @property
    def rowcount(self) -> int:
        return int(getattr(self._cursor, "rowcount", -1))

    @property
    def lastrowid(self) -> Any:
        # PostgreSQL has no connection-scoped last-row identity. Repository
        # inserts should supply UUID/text keys or use RETURNING explicitly.
        return getattr(self._cursor, "lastrowid", None)

    @property
    def description(self) -> Any:
        return self._cursor.description

    def _row(self, value: Any) -> DatabaseRow | None:
        if value is None:
            return None
        columns = [item[0] for item in (self._cursor.description or ())]
        return DatabaseRow(columns, value)

    def fetchone(self) -> DatabaseRow | None:
        return self._row(self._cursor.fetchone())

    def fetchall(self) -> list[DatabaseRow]:
        return [self._row(row) for row in self._cursor.fetchall()]  # type: ignore[misc]

    def __iter__(self) -> Iterator[DatabaseRow]:
        for row in self._cursor:
            wrapped = self._row(row)
            if wrapped is not None:
                yield wrapped

    def execute(self, sql: str, parameters: Sequence[Any] | None = None) -> ServerCursor:
        bound = tuple(parameters or ())
        self._cursor.execute(_translate_sql(sql, len(bound)), bound)
        return self

    def close(self) -> None:
        self._cursor.close()


class PostgreSQLConnection:
    """MMFDB connection protocol over a SQLAlchemy-managed PostgreSQL DB-API handle."""

    dialect = "postgresql"

    def __init__(self, raw_connection: Any, *, engine: Any = None) -> None:
        self._raw_connection = raw_connection
        self._engine = engine
        self._closed = False
        self._context_transactions: list[str | None] = []

    @property
    def in_transaction(self) -> bool:
        driver = getattr(self._raw_connection, "driver_connection", self._raw_connection)
        info = getattr(driver, "info", None)
        status = getattr(info, "transaction_status", None)
        if status is not None:
            # psycopg.pq.TransactionStatus.IDLE has integer value zero.
            return int(status) != 0
        status = getattr(driver, "status", None)
        if status is not None:
            # psycopg2.extensions.STATUS_READY has integer value one.
            return int(status) != 1
        return bool(getattr(driver, "in_transaction", False))

    def execute(
        self, sql: str, parameters: Sequence[Any] | None = None
    ) -> ServerCursor:
        bound = tuple(parameters or ())
        translated = _translate_sql(sql, len(bound))
        cursor = self._raw_connection.cursor()
        try:
            cursor.execute(translated, bound)
        except Exception:
            cursor.close()
            raise
        return ServerCursor(cursor)

    def cursor(self) -> ServerCursor:
        return ServerCursor(self._raw_connection.cursor())

    def commit(self) -> None:
        self._raw_connection.commit()

    def rollback(self) -> None:
        self._raw_connection.rollback()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._raw_connection.close()
        finally:
            if self._engine is not None:
                self._engine.dispose()

    def __enter__(self) -> PostgreSQLConnection:
        if self.in_transaction or self._context_transactions:
            savepoint = f"mmfdb_ctx_{uuid.uuid4().hex}"
            cursor = self.execute(f"SAVEPOINT {savepoint}")
            cursor.close()
            self._context_transactions.append(savepoint)
        else:
            cursor = self.execute("BEGIN")
            cursor.close()
            self._context_transactions.append(None)
        return self

    def __exit__(
        self, exc_type: Any, exc: BaseException | None, tb: Any
    ) -> Literal[False]:
        marker = self._context_transactions.pop()
        if marker is None:
            if exc is None:
                self.commit()
            else:
                self.rollback()
        else:
            if exc is None:
                cursor = self.execute(f"RELEASE SAVEPOINT {marker}")
                cursor.close()
            else:
                cursor = self.execute(f"ROLLBACK TO SAVEPOINT {marker}")
                cursor.close()
                cursor = self.execute(f"RELEASE SAVEPOINT {marker}")
                cursor.close()
        return False


_UNSUPPORTED_SQL = (
    (re.compile(r"^\s*PRAGMA\b", re.IGNORECASE), "SQLite PRAGMA"),
    (re.compile(r"\bsqlite_master\b", re.IGNORECASE), "sqlite_master"),
    (re.compile(r"\bINSERT\s+OR\s+(?:IGNORE|REPLACE)\b", re.IGNORECASE), "SQLite INSERT OR"),
    (re.compile(r"\bAUTOINCREMENT\b", re.IGNORECASE), "SQLite AUTOINCREMENT"),
    (re.compile(r"\bGLOB\b", re.IGNORECASE), "SQLite GLOB"),
)


def _translate_sql(sql: str, parameter_count: int | None = None) -> str:
    """Validate portable SQL and translate qmark parameters to ``%s``."""
    for pattern, feature in _UNSUPPORTED_SQL:
        if pattern.search(sql):
            raise DatabaseCapabilityError(
                f"{feature} SQL is not supported by the PostgreSQL backend; "
                "use a dialect-specific repository operation"
            )
    if parameter_count == 0:
        # A bare question mark is also PostgreSQL's JSONB existence operator.
        # With no bound values there is no placeholder to translate.
        return sql
    translated, replacements = _qmark_to_format(sql, _return_count=True)
    if parameter_count is not None and replacements != parameter_count:
        raise DatabaseBackendError(
            "SQL qmark placeholder count does not match bound parameter count "
            f"({replacements} placeholders, {parameter_count} values). If the "
            "statement uses PostgreSQL '?' operators, move it behind a "
            "dialect-specific repository method."
        )
    return translated


@overload
def _qmark_to_format(sql: str, *, _return_count: Literal[True]) -> tuple[str, int]: ...


@overload
def _qmark_to_format(sql: str, *, _return_count: Literal[False] = False) -> str: ...


def _qmark_to_format(
    sql: str, *, _return_count: bool = False
) -> str | tuple[str, int]:
    """Replace parameter qmarks outside quoted strings and SQL comments."""
    placeholder_sentinel = "\x00MMFDB_PARAMETER\x00"
    result: list[str] = []
    index = 0
    state = "code"
    dollar_tag = ""
    block_depth = 0
    replacements = 0
    while index < len(sql):
        char = sql[index]
        pair = sql[index : index + 2]
        if state == "code":
            if pair == "--":
                state = "line_comment"
                result.append(pair)
                index += 2
                continue
            if pair == "/*":
                state = "block_comment"
                block_depth = 1
                result.append(pair)
                index += 2
                continue
            if char in {"'", '"'}:
                state = "single_quote" if char == "'" else "double_quote"
                result.append(char)
                index += 1
                continue
            if char == "$":
                match = re.match(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$", sql[index:])
                if match:
                    dollar_tag = match.group(0)
                    state = "dollar_quote"
                    result.append(dollar_tag)
                    index += len(dollar_tag)
                    continue
            if char == "?":
                result.append(placeholder_sentinel)
                replacements += 1
            else:
                result.append(char)
            index += 1
            continue
        if state == "line_comment":
            result.append(char)
            index += 1
            if char == "\n":
                state = "code"
            continue
        if state == "block_comment":
            if pair == "/*":
                block_depth += 1
                result.append(pair)
                index += 2
            elif pair == "*/":
                block_depth -= 1
                result.append(pair)
                index += 2
                if block_depth == 0:
                    state = "code"
            else:
                result.append(char)
                index += 1
            continue
        if state in {"single_quote", "double_quote"}:
            quote = "'" if state == "single_quote" else '"'
            result.append(char)
            index += 1
            if state == "single_quote" and char == "\\" and index < len(sql):
                # Be conservative for PostgreSQL E'...' strings. Treating the
                # next byte as quoted is safe even when standard strings are in
                # use and prevents accidental placeholder rewriting.
                result.append(sql[index])
                index += 1
                continue
            if char == quote:
                if index < len(sql) and sql[index] == quote:
                    result.append(sql[index])
                    index += 1
                else:
                    state = "code"
            continue
        if state == "dollar_quote":
            if sql.startswith(dollar_tag, index):
                result.append(dollar_tag)
                index += len(dollar_tag)
                state = "code"
            else:
                result.append(char)
                index += 1
    # Psycopg's format-style binding requires every literal percent sign to be
    # doubled whenever parameters are supplied, including percent signs inside
    # quoted LIKE patterns. Preserve only the placeholders created above.
    translated = "".join(result).replace("%", "%%").replace(
        placeholder_sentinel, "%s"
    )
    return (translated, replacements) if _return_count else translated


def connect_postgresql(target: DatabaseTarget, *, readonly: bool = False) -> PostgreSQLConnection:
    """Connect to a PostgreSQL URL through MMFDB's optional SQL dependency."""
    if target.dialect != "postgresql":
        raise ValueError("connect_postgresql requires a PostgreSQL target")
    try:
        from sqlalchemy import create_engine
        from sqlalchemy.exc import NoSuchModuleError
    except ImportError as exc:  # pragma: no cover - depends on installation extras
        raise DatabaseDriverError(
            "PostgreSQL support requires the 'postgres' extra: pip install mmfdb[postgres]"
        ) from exc

    url = target.location
    if url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[len("postgres://") :]
    elif url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://") :]
    engine = None
    try:
        engine = create_engine(url, future=True, pool_pre_ping=True)
        raw = engine.raw_connection()
    except (ImportError, ModuleNotFoundError, NoSuchModuleError) as exc:
        if engine is not None:
            engine.dispose()
        raise DatabaseDriverError(
            "PostgreSQL support requires the 'postgres' extra and a compatible "
            "driver: pip install mmfdb[postgres]"
        ) from exc
    except Exception:
        if engine is not None:
            engine.dispose()
        # Authentication, TLS and network failures retain the driver's useful
        # diagnostics; importantly, they are never converted to a SQLite path.
        raise

    driver = cast(Any, getattr(raw, "driver_connection", raw))
    if hasattr(driver, "autocommit"):
        driver.autocommit = True
    connection = PostgreSQLConnection(raw, engine=engine)
    if readonly:
        cursor = connection.execute("SET default_transaction_read_only = on")
        cursor.close()
    return connection


def connection_dialect(connection: Any) -> str:
    """Return the normalized dialect for a supported connection object."""
    if isinstance(connection, sqlite3.Connection):
        return "sqlite"
    dialect = getattr(connection, "dialect", None)
    if dialect in {"sqlite", "postgresql"}:
        return str(dialect)
    raise UnsupportedDatabaseBackend(
        "Cannot infer database dialect from borrowed connection; wrap server "
        "connections with PostgreSQLConnection"
    )


def postgres_schema_version(connection: PostgreSQLConnection) -> int:
    """Read the version from a provisioned PostgreSQL MMFDB schema."""
    rows = connection.execute(
        """SELECT table_name FROM information_schema.tables
           WHERE table_schema = current_schema()
             AND table_name IN ('mmfdb_schema_version', '_schema_version')"""
    ).fetchall()
    names = {str(row[0]) for row in rows}
    for table in ("mmfdb_schema_version", "_schema_version"):
        if table in names:
            row = connection.execute(f'SELECT version FROM "{table}"').fetchone()
            if row is not None:
                return int(row[0])
    return 0


def validate_postgresql_schema(connection: PostgreSQLConnection) -> None:
    """Reject version-stamped but structurally incomplete server schemas."""
    required = {
        "flr_sample",
        "flr_sample_users",
        "mmfdb_artifact",
        "mmfdb_operation",
        "mmfdb_object_acl",
        "mmfdb_audit_log",
    }
    rows = connection.execute(
        """SELECT table_name FROM information_schema.tables
           WHERE table_schema = current_schema()
           ORDER BY table_name"""
    ).fetchall()
    present = {str(row[0]) for row in rows}
    missing = sorted(required - present)
    if missing:
        raise DatabaseCapabilityError(
            "PostgreSQL MMFDB schema is incomplete despite its version stamp; "
            f"missing required table(s): {', '.join(missing)}"
        )
