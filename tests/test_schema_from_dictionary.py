"""Tests for schema_from_dictionary.py — DDL generation from the .dic.

Verifies that:
1. Generated CREATE TABLE contains expected columns, types, PK, FK.
2. A DB built via the generator has the same columns the .dic declares
   (round-trip with ``introspect_sqlite_schema``).
"""

from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest

from mmfdb.schema.dictionary_schema_map import introspect_sqlite_schema
from mmfdb.schema.pdbx_metadata import MmcifDictionary
from mmfdb.schema.schema_from_dictionary import (
    TYPE_CODE_SQL_MAP,
    generate_create_table_for_category,
    generate_index_for_table,
)


def _dic() -> MmcifDictionary:
    return MmcifDictionary.load_bundled()


def _type_code_to_sql(tc: str) -> str:
    return TYPE_CODE_SQL_MAP.get(tc.lower(), "TEXT")


def _expected_sql_type(item) -> str:
    return _type_code_to_sql(item.type_code or "line")


def test_detector_channel_ddl_has_expected_columns() -> None:
    """Generated DDL for mmfdb_setup_detector_channel contains all .dic columns."""
    dic = _dic()
    ddl = generate_create_table_for_category(dic, "mmfdb_setup_detector_channel")
    assert "CREATE TABLE IF NOT EXISTS mmfdb_setup_detector_channel" in ddl

    cat = dic.get_category("mmfdb_setup_detector_channel")
    for item in cat.items.values():
        col = item.schema_column or item.attribute
        assert col in ddl, f"Column {col!r} missing from DDL"
        sql_type = _expected_sql_type(item)
        assert sql_type in ddl.split(col)[1].split()[0], (
            f"Column {col} has wrong type (expected {sql_type})"
        )

    # PK
    assert "id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT" in ddl
    # FK (setup_id)
    assert "REFERENCES mmfdb_setup(setup_id) ON DELETE CASCADE" in ddl
    # Audit columns
    assert "created_at" in ddl
    assert "updated_at" in ddl
    assert "deleted_at" in ddl


def test_pie_window_ddl_has_expected_columns() -> None:
    """Generated DDL for mmfdb_setup_pie_window contains all .dic columns."""
    dic = _dic()
    ddl = generate_create_table_for_category(dic, "mmfdb_setup_pie_window")
    assert "CREATE TABLE IF NOT EXISTS mmfdb_setup_pie_window" in ddl

    cat = dic.get_category("mmfdb_setup_pie_window")
    for item in cat.items.values():
        col = item.schema_column or item.attribute
        assert col in ddl, f"Column {col!r} missing from DDL"

    # PK
    assert "id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT" in ddl
    # FK
    assert "REFERENCES mmfdb_setup(setup_id) ON DELETE CASCADE" in ddl
    # NOT NULL on mandatory items
    assert "end INTEGER NOT NULL" in ddl
    assert "start INTEGER NOT NULL" in ddl


def test_round_trip_ddl_builds_db_with_dic_columns() -> None:
    """A DB built via the generated DDL has exactly the columns the .dic declares."""
    dic = _dic()
    tmpdir = tempfile.mkdtemp()
    db_path = os.path.join(tmpdir, "test_roundtrip.db")

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Create mmfdb_setup first (parent table for FK)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS mmfdb_setup (
            setup_id TEXT PRIMARY KEY,
            name TEXT NOT NULL
        )
    """)
    # Create the child tables from generated DDL
    for cat_name in ["mmfdb_setup_detector_channel", "mmfdb_setup_pie_window"]:
        ddl = generate_create_table_for_category(dic, cat_name)
        cursor.execute(ddl)

    conn.commit()
    conn.close()

    # Introspect the live schema
    schema = introspect_sqlite_schema(db_path)

    for cat_name in ["mmfdb_setup_detector_channel", "mmfdb_setup_pie_window"]:
        cat = dic.get_category(cat_name)
        table_name = cat_name
        assert table_name in schema, f"Table {table_name} not found in DB"

        live_cols = set(schema[table_name].keys())
        for item in cat.items.values():
            col = item.schema_column or item.attribute
            assert col in live_cols, (
                f"Column {table_name}.{col} declared in .dic but missing in live DB"
            )
            actual_type = schema[table_name][col]["type"]
            expected_type = _expected_sql_type(item)
            assert actual_type == expected_type, (
                f"Column {table_name}.{col} type mismatch: "
                f"expected {expected_type}, got {actual_type}"
            )

        # Audit columns should be present
        for audit in ["created_at", "updated_at", "deleted_at"]:
            assert audit in live_cols, (
                f"Audit column {table_name}.{audit} missing from live DB"
            )


def test_generate_index() -> None:
    """generate_index_for_table produces valid DDL."""
    ddl = generate_index_for_table("mmfdb_setup_detector_channel", "setup_id")
    assert ddl == (
        "CREATE INDEX IF NOT EXISTS idx_mmfdb_setup_detector_channel_setup_id "
        "ON mmfdb_setup_detector_channel (setup_id)"
    )


def test_unknown_category_raises_instead_of_emitting_a_comment():
    """A missing category must fail loudly, not degrade to a no-op comment.

    The DDL for the dictionary-generated tables is built into
    ``CREATE_TABLES_SQL`` at import time. Returning a SQL comment for an
    unknown category meant the statement executed as a no-op, so the database
    stamped itself as migrated while silently missing a table -- surfacing much
    later as an unrelated "no such table".
    """
    from mmfdb.schema.schema_from_dictionary import UnknownCategoryError

    dic = MmcifDictionary.load_bundled()
    with pytest.raises(UnknownCategoryError, match="mmfdb_no_such_category"):
        generate_create_table_for_category(dic, "mmfdb_no_such_category")


def test_every_dictionary_generated_migration_table_resolves():
    """Each category CREATE_TABLES_SQL generates must exist in the dictionary.

    This is the guard for the failure above: it caught a stale
    ``_get_dict_ddl("mmfdb_microtime_shift")`` whose category had been removed
    by the PRD-19 collapse, so that entry had silently been a comment while the
    same module's ``_drop_legacy_tables`` dropped the table.
    """
    import re
    from pathlib import Path

    import mmfdb.schema.schema as schema_mod

    source = Path(schema_mod.__file__).read_text(encoding="utf-8")
    categories = re.findall(r'_get_dict_ddl\(\s*"([^"]+)"\s*\)', source)
    assert categories, "no _get_dict_ddl call sites found -- has the pattern changed?"

    dic = MmcifDictionary.load_bundled()
    missing = [c for c in categories if dic.get_category(c) is None]
    assert not missing, f"CREATE_TABLES_SQL asks for undeclared categories: {missing}"


def test_generated_tables_are_actually_created_in_a_fresh_database(tmp_path):
    """End-to-end: a fresh database really contains the generated tables."""
    from mmfdb.repository import MFDatabase

    db = MFDatabase(os.path.join(tmp_path, "fresh.db"))
    try:
        live = {
            row[0]
            for row in db.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        for table in (
            "mmfdb_setup_detector_channel",
            "mmfdb_setup_pie_window",
            "mmfdb_setup_fcs_pair",
            "mmfdb_setup_calibration",
            "mmfdb_artifact_owner",
        ):
            assert table in live, f"{table} was not created"
        # Retired by PRD-19; _drop_legacy_tables removes it.
        assert "mmfdb_microtime_shift" not in live
    finally:
        db.close()
