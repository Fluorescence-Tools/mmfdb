"""Dictionary-/schema-driven data access (PRD-26 Task 1).

The ``.dic`` already dictates the schema (PRD-19). This module is the generated
**DAO core** that the hand-written repository SQL can migrate onto: a generic,
fully-parameterised CRUD layer whose table/column identifiers are **whitelisted
against the live schema** (so identifiers can never carry caller input) and whose
values are always bound parameters (no f-string interpolation).

It is intentionally generic — one ``DictionaryDao`` works for every
dictionary-declared table — and consistent: soft-delete (``deleted_at``) and an
``updated_at`` touch are applied automatically when the table declares those
columns. Bespoke queries (lineage, browse, joins) stay hand-written; this layer
covers the CRUD-shaped majority.

Example
-------
>>> dao = DictionaryDao.from_connection(conn)
>>> dao.insert("flr_sample", {"sample_id": "s1", "description": "DNA"})
>>> dao.get("flr_sample", "s1")
{'sample_id': 's1', 'description': 'DNA', ...}
>>> dao.update("flr_sample", "s1", {"description": "DNA duplex"})
>>> dao.soft_delete("flr_sample", "s1")
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from mmfdb.schema.dictionary_schema_map import (
    DictionarySchemaMap,
    introspect_sqlite_schema,
    quote_identifier,
)

#: Conventional columns the DAO applies automatically when a table declares them.
SOFT_DELETE_COLUMN = "deleted_at"
UPDATED_AT_COLUMN = "updated_at"


class DaoError(Exception):
    """Base error for dictionary-driven data access."""


class UnknownTableError(DaoError):
    """Raised when a table is not part of the live/declared schema."""


class UnknownColumnError(DaoError):
    """Raised when a column is not declared for the target table."""


def _introspect_postgresql_schema(
    conn: Any,
) -> dict[str, dict[str, dict[str, Any]]]:
    """Return the current PostgreSQL schema in the DAO's canonical shape."""
    primary_key_rows = conn.execute(
        """SELECT kcu.table_name, kcu.column_name, kcu.ordinal_position
           FROM information_schema.table_constraints AS tc
           JOIN information_schema.key_column_usage AS kcu
             ON tc.constraint_name = kcu.constraint_name
            AND tc.constraint_schema = kcu.constraint_schema
          WHERE tc.constraint_type = 'PRIMARY KEY'
            AND tc.table_schema = current_schema()"""
    ).fetchall()
    primary_keys = {
        (row[0], row[1]): int(row[2]) for row in primary_key_rows
    }
    rows = conn.execute(
        """SELECT table_name, column_name, ordinal_position, data_type,
                  is_nullable, column_default
             FROM information_schema.columns
            WHERE table_schema = current_schema()
            ORDER BY table_name, ordinal_position"""
    ).fetchall()
    schema: dict[str, dict[str, dict[str, Any]]] = {}
    for row in rows:
        table, column = str(row[0]), str(row[1])
        pk_position = primary_keys.get((table, column), 0)
        schema.setdefault(table, {})[column] = {
            "cid": int(row[2]) - 1,
            "type": str(row[3]),
            "notnull": str(row[4]).upper() == "NO",
            "default": row[5],
            "primary_key": bool(pk_position),
            "primary_key_position": pk_position,
        }
    return schema


class DictionaryDao:
    """Generic, parameterised CRUD over dictionary-declared MMFDB tables.

    Parameters
    ----------
    conn : sqlite3.Connection
        Open connection. ``row_factory`` is set to ``sqlite3.Row`` if unset so
        rows are returned as dicts.
    schema : Mapping[str, Mapping[str, Mapping[str, Any]]]
        ``{table: {column: column_meta}}`` as produced by
        :func:`introspect_sqlite_schema`. The keys define the identifier
        whitelist; values carry ``primary_key``/``notnull``/``type`` metadata.
    """

    def __init__(
        self,
        conn: Any,
        schema: Mapping[str, Mapping[str, Mapping[str, Any]]],
    ) -> None:
        self.conn = conn
        if hasattr(conn, "row_factory") and getattr(conn, "row_factory", None) is None:
            import sqlite3

            conn.row_factory = sqlite3.Row
        self._schema = schema

    # -- constructors ----------------------------------------------------

    @classmethod
    def from_connection(cls, conn: Any) -> "DictionaryDao":
        """Build a DAO by introspecting the live schema of ``conn``."""
        if getattr(conn, "dialect", "sqlite") == "postgresql":
            return cls(conn, _introspect_postgresql_schema(conn))
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        schema: dict[str, dict[str, dict[str, Any]]] = {}
        for row in rows:
            name = row[0]
            columns: dict[str, dict[str, Any]] = {}
            for cid, col, col_type, notnull, default, pk in conn.execute(
                f"PRAGMA table_info({quote_identifier(name)})"
            ):
                columns[col] = {
                    "cid": cid,
                    "type": col_type,
                    "notnull": bool(notnull),
                    "default": default,
                    "primary_key": bool(pk),
                    "primary_key_position": int(pk),
                }
            schema[name] = columns
        return cls(conn, schema)

    @classmethod
    def from_dictionary_map(
        cls, conn: Any, schema_map: DictionarySchemaMap
    ) -> "DictionaryDao":
        """Build a DAO from a :class:`DictionarySchemaMap`'s live schema.

        Falls back to introspecting ``conn`` when the map carries no live schema.
        """
        if schema_map.schema:
            return cls(conn, schema_map.schema)
        return cls.from_connection(conn)

    @classmethod
    def from_db_path(cls, conn: Any, db_path: str) -> "DictionaryDao":
        """Build a DAO whose schema is introspected from ``db_path``."""
        return cls(conn, introspect_sqlite_schema(db_path))

    # -- schema helpers --------------------------------------------------

    def has_table(self, table: str) -> bool:
        """Return ``True`` if ``table`` is part of the declared schema."""
        return table in self._schema

    def columns(self, table: str) -> set[str]:
        """Return the declared column names for ``table``."""
        self._require_table(table)
        return set(self._schema[table])

    def primary_key(self, table: str) -> str:
        """Return the primary-key column for ``table``.

        Falls back to ``<table>_id`` / ``id`` when no PRAGMA primary key is set.
        """
        self._require_table(table)
        keys = self.primary_keys(table)
        if len(keys) > 1:
            raise DaoError(
                f"Table {table!r} has a composite primary key {keys!r}; "
                "use primary_keys() or pass all key values."
            )
        if keys:
            return keys[0]
        cols = self._schema[table]
        for candidate in (f"{table}_id", "id", "uuid"):
            if candidate in cols:
                return candidate
        raise DaoError(f"No primary key found for table {table!r}.")

    def primary_keys(self, table: str) -> tuple[str, ...]:
        """Return every declared primary-key column in SQLite key order."""
        self._require_table(table)
        keyed = [
            (int(meta.get("primary_key_position") or 0), name)
            for name, meta in self._schema[table].items()
            if meta.get("primary_key")
        ]
        keyed.sort(key=lambda item: (item[0] or 10_000, item[1]))
        return tuple(name for _, name in keyed)

    def _key_clause(
        self,
        table: str,
        key_value: Any,
        key_columns: str | Iterable[str] | None,
    ) -> tuple[list[str], list[Any]]:
        if key_columns is None:
            keys = list(self.primary_keys(table))
            if not keys:
                keys = [self.primary_key(table)]
        elif isinstance(key_columns, str):
            keys = [key_columns]
        else:
            keys = list(key_columns)
        self._require_columns(table, keys)
        if not keys:
            raise DaoError(f"No key columns supplied for table {table!r}.")

        if isinstance(key_value, Mapping):
            missing = [key for key in keys if key not in key_value]
            if missing:
                raise DaoError(f"Missing key value(s) for {table!r}: {missing!r}.")
            values = [key_value[key] for key in keys]
        elif len(keys) == 1:
            values = [key_value]
        else:
            if isinstance(key_value, (str, bytes)):
                raise DaoError(f"Composite key for {table!r} requires {len(keys)} values.")
            try:
                values = list(key_value)
            except TypeError as exc:
                raise DaoError(
                    f"Composite key for {table!r} requires {len(keys)} values."
                ) from exc
            if len(values) != len(keys):
                raise DaoError(f"Composite key for {table!r} requires {len(keys)} values.")
        return keys, values

    def _require_table(self, table: str) -> None:
        if table not in self._schema:
            raise UnknownTableError(f"Unknown table: {table!r}.")

    def _require_columns(self, table: str, names: Iterable[str]) -> None:
        declared = self._schema[table]
        unknown = [n for n in names if n not in declared]
        if unknown:
            raise UnknownColumnError(
                f"Unknown column(s) for {table!r}: {sorted(unknown)}."
            )

    def _has(self, table: str, column: str) -> bool:
        return column in self._schema.get(table, {})

    # -- CRUD ------------------------------------------------------------

    def insert(self, table: str, values: Mapping[str, Any]) -> str | int | None:
        """Insert one row; return its primary-key value (or last rowid).

        All identifiers are whitelisted against the schema and all values are
        bound parameters. Works on keyless tables (e.g. UNIQUE-only junctions):
        with no resolvable primary key the last rowid is returned.
        """
        self._require_table(table)
        if not values:
            raise DaoError(f"insert into {table!r} requires at least one value.")
        self._require_columns(table, values.keys())

        cols = list(values.keys())
        placeholders = ", ".join("?" for _ in cols)
        col_sql = ", ".join(quote_identifier(c) for c in cols)
        sql = (
            f"INSERT INTO {quote_identifier(table)} ({col_sql}) VALUES ({placeholders})"
        )
        cur = self.conn.execute(sql, [values[c] for c in cols])
        try:
            pk = self.primary_key(table)
        except DaoError:
            return cur.lastrowid
        if pk in values:
            return values[pk]
        return cur.lastrowid

    def upsert(
        self,
        table: str,
        values: Mapping[str, Any],
        *,
        conflict: str | Iterable[str] | None = None,
        touch: bool = True,
    ) -> str | int | None:
        """Insert one row, or update it in place when it already exists.

        Uses SQLite ``INSERT ... ON CONFLICT(<target>) DO UPDATE`` — an
        identity-preserving upsert. Unlike ``INSERT OR REPLACE`` it does **not**
        delete-and-reinsert the conflicting row, so its primary key and any rows
        that foreign-key to it survive, and columns not present in *values* keep
        their existing content. All identifiers are whitelisted against the schema
        and all values are bound parameters.

        Parameters
        ----------
        table : str
            Target table; must be part of the declared schema.
        values : Mapping[str, Any]
            Column-to-value pairs to insert or update.
        conflict : str or Iterable[str], optional
            Conflict-target column(s). Defaults to the table's primary key.
        touch : bool, default True
            When the table declares an ``updated_at`` column and *values* does not
            set it, refresh it to ``CURRENT_TIMESTAMP`` on the update branch.

        Returns
        -------
        str or int or None
            The primary-key value from *values* if present, else the last rowid.
        """
        self._require_table(table)
        if not values:
            raise DaoError(f"upsert into {table!r} requires at least one value.")
        self._require_columns(table, values.keys())

        if conflict is None:
            conflict_cols = [self.primary_key(table)]
        elif isinstance(conflict, str):
            conflict_cols = [conflict]
        else:
            conflict_cols = list(conflict)
        self._require_columns(table, conflict_cols)

        cols = list(values.keys())
        placeholders = ", ".join("?" for _ in cols)
        col_sql = ", ".join(quote_identifier(c) for c in cols)
        conflict_sql = ", ".join(quote_identifier(c) for c in conflict_cols)

        update_cols = [c for c in cols if c not in conflict_cols]
        assignments = [
            f"{quote_identifier(c)} = excluded.{quote_identifier(c)}" for c in update_cols
        ]
        if touch and self._has(table, UPDATED_AT_COLUMN) and UPDATED_AT_COLUMN not in values:
            assignments.append(f"{quote_identifier(UPDATED_AT_COLUMN)} = CURRENT_TIMESTAMP")

        if assignments:
            action = f"DO UPDATE SET {', '.join(assignments)}"
        else:
            action = "DO NOTHING"
        sql = (
            f"INSERT INTO {quote_identifier(table)} ({col_sql}) VALUES ({placeholders}) "
            f"ON CONFLICT({conflict_sql}) {action}"
        )
        cur = self.conn.execute(sql, [values[c] for c in cols])
        try:
            pk = self.primary_key(table)
        except DaoError:
            # Keyless table (no PRIMARY KEY, no <table>_id/id/uuid): an explicit
            # ``conflict`` target — e.g. a UNIQUE(sample_id, key) junction — is a
            # valid upsert even though there is no resolvable primary key.
            return cur.lastrowid
        if pk in values:
            return values[pk]
        return cur.lastrowid

    def get(
        self,
        table: str,
        pk_value: Any,
        *,
        pk_column: str | Iterable[str] | None = None,
        include_deleted: bool = False,
    ) -> dict[str, Any] | None:
        """Return one row by primary key as a dict, or ``None``."""
        self._require_table(table)
        keys, params = self._key_clause(table, pk_value, pk_column)
        predicate = " AND ".join(f"{quote_identifier(key)} = ?" for key in keys)
        sql = f"SELECT * FROM {quote_identifier(table)} WHERE {predicate}"
        if not include_deleted and self._has(table, SOFT_DELETE_COLUMN):
            sql += f" AND {quote_identifier(SOFT_DELETE_COLUMN)} IS NULL"
        row = self.conn.execute(sql, params).fetchone()
        return dict(row) if row is not None else None

    def list(
        self,
        table: str,
        *,
        filters: Mapping[str, Any] | None = None,
        include_deleted: bool = False,
        order_by: str | None = None,
        descending: bool = False,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return rows matching equality ``filters`` (all parameterised)."""
        self._require_table(table)
        clauses: list[str] = []
        params: list[Any] = []
        if filters:
            self._require_columns(table, filters.keys())
            for col, val in filters.items():
                if val is None:
                    clauses.append(f"{quote_identifier(col)} IS NULL")
                else:
                    clauses.append(f"{quote_identifier(col)} = ?")
                    params.append(val)
        if not include_deleted and self._has(table, SOFT_DELETE_COLUMN):
            clauses.append(f"{quote_identifier(SOFT_DELETE_COLUMN)} IS NULL")

        sql = f"SELECT * FROM {quote_identifier(table)}"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        if order_by is not None:
            self._require_columns(table, [order_by])
            sql += f" ORDER BY {quote_identifier(order_by)}"
            sql += " DESC" if descending else " ASC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
            if offset is not None:
                sql += " OFFSET ?"
                params.append(int(offset))
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def update(
        self,
        table: str,
        pk_value: Any,
        values: Mapping[str, Any],
        *,
        pk_column: str | Iterable[str] | None = None,
        touch: bool = True,
    ) -> int:
        """Update one row by primary key; return the affected row count.

        Sets ``updated_at`` to ``CURRENT_TIMESTAMP`` when the table declares it
        and ``touch`` is true. The primary-key column cannot be updated.
        """
        self._require_table(table)
        if not values:
            return 0
        self._require_columns(table, values.keys())
        keys, key_values = self._key_clause(table, pk_value, pk_column)
        changed_keys = sorted(set(keys).intersection(values))
        if changed_keys:
            raise DaoError(f"Cannot update primary key column(s) {changed_keys!r} of {table!r}.")

        set_cols = list(values.keys())
        assignments = [f"{quote_identifier(c)} = ?" for c in set_cols]
        params: list[Any] = [values[c] for c in set_cols]
        if touch and self._has(table, UPDATED_AT_COLUMN):
            assignments.append(f"{quote_identifier(UPDATED_AT_COLUMN)} = CURRENT_TIMESTAMP")
        sql = (
            f"UPDATE {quote_identifier(table)} SET {', '.join(assignments)} "
            "WHERE " + " AND ".join(f"{quote_identifier(key)} = ?" for key in keys)
        )
        params.extend(key_values)
        if self._has(table, SOFT_DELETE_COLUMN):
            sql += f" AND {quote_identifier(SOFT_DELETE_COLUMN)} IS NULL"
        return int(self.conn.execute(sql, params).rowcount)

    def soft_delete(
        self,
        table: str,
        pk_value: Any,
        *,
        pk_column: str | Iterable[str] | None = None,
        deleted_at: str | None = None,
    ) -> int:
        """Soft-delete one row (set ``deleted_at``); return affected row count.

        ``deleted_at`` defaults to SQLite ``CURRENT_TIMESTAMP``; pass an explicit
        value (e.g. an application ``_utc_now()`` ISO string) to control the stored
        marker. Falls back to a hard ``DELETE`` for tables without a ``deleted_at``
        column.
        """
        self._require_table(table)
        keys, key_values = self._key_clause(table, pk_value, pk_column)
        predicate = " AND ".join(f"{quote_identifier(key)} = ?" for key in keys)
        if not self._has(table, SOFT_DELETE_COLUMN):
            sql = f"DELETE FROM {quote_identifier(table)} WHERE {predicate}"
            return int(self.conn.execute(sql, key_values).rowcount)
        params: list[Any] = []
        if deleted_at is None:
            set_clause = f"{quote_identifier(SOFT_DELETE_COLUMN)} = CURRENT_TIMESTAMP"
        else:
            set_clause = f"{quote_identifier(SOFT_DELETE_COLUMN)} = ?"
            params.append(deleted_at)
        sql = (
            f"UPDATE {quote_identifier(table)} SET {set_clause} "
            f"WHERE {predicate} "
            f"AND {quote_identifier(SOFT_DELETE_COLUMN)} IS NULL"
        )
        params.extend(key_values)
        return int(self.conn.execute(sql, params).rowcount)
