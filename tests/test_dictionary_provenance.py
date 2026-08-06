"""The dictionary says which revision it is, and a record says which it used.

MMFDB validates against enumerations read out of ``mmfdb_flr_ext.dic``, so a
revision of that file can rename or withdraw a term that rows are already
tagged with. Before this, nothing noticed: the local dictionary declared no
version at all, upstream versions were parsed past and discarded, and the parse
cache keyed on mtime -- so a dictionary re-downloaded with a preserved
timestamp left a stale cache in place and the vocabulary silently disagreed
with the file that defines it.

These tests pin the three things that make drift visible rather than silent:
the declaration, the content hash, and the stamp on a written row.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path

import pytest

from mmfdb.schema import schema
from mmfdb.schema.pdbx_metadata import (
    EXTENSION_DICT,
    MmcifDictionary,
    extension_dictionary_hash,
    extension_dictionary_version,
)


@pytest.fixture
def dic() -> MmcifDictionary:
    MmcifDictionary._cached_dict = None
    return MmcifDictionary.load_bundled()


# -- the dictionaries declare themselves -------------------------------------


def test_the_local_extension_declares_a_version(dic):
    """It declared none, which is why nothing downstream could cite one."""
    assert dic.dictionary_version(EXTENSION_DICT), (
        f"{EXTENSION_DICT} declares no _dictionary.version"
    )


def test_every_bundled_dictionary_reports_a_version_and_a_hash(dic):
    provenance = dic.provenance()
    assert set(provenance) == set(MmcifDictionary.BUNDLED_DICTS)
    for name, info in provenance.items():
        assert info["version"], f"{name} declares no version"
        assert len(info["sha256"]) == 64, f"{name} has no content hash"


def test_the_hash_is_of_the_bytes_on_disk(dic):
    """Checked against hashlib rather than against our own function, so the
    lockfile written by the shell script and the value recorded in Python are
    the same number by construction."""
    import hashlib

    path = MmcifDictionary._resolve_dic(EXTENSION_DICT)
    expected = hashlib.sha256(path.read_bytes()).hexdigest()
    assert dic.dictionary_hash(EXTENSION_DICT) == expected


# -- the cache invalidates on content, not on a timestamp ---------------------


def test_a_content_change_with_a_preserved_mtime_invalidates_the_cache():
    """The failure the mtime check could not see. A dictionary re-downloaded
    by a tool that restores timestamps kept the old parse forever."""
    path = MmcifDictionary._resolve_dic(EXTENSION_DICT)
    stat = os.stat(path)
    original = path.read_bytes()

    MmcifDictionary._cached_dict = None
    MmcifDictionary.load_bundled().save_cache()
    MmcifDictionary._cached_dict = None
    assert MmcifDictionary._load_cache_if_valid() is not None, "cache did not round trip"

    try:
        path.write_bytes(original + b"\n# a revision that keeps its mtime\n")
        os.utime(path, (stat.st_atime, stat.st_mtime))
        assert os.stat(path).st_mtime == stat.st_mtime, "the test did not preserve the mtime"

        MmcifDictionary._cached_dict = None
        assert MmcifDictionary._load_cache_if_valid() is None, (
            "a changed dictionary was served from a stale cache"
        )
    finally:
        path.write_bytes(original)
        os.utime(path, (stat.st_atime, stat.st_mtime))
        MmcifDictionary._cached_dict = None
        MmcifDictionary.load_bundled().save_cache()


# -- the upstream pin ---------------------------------------------------------


def test_the_lockfile_pins_every_upstream_dictionary(dic):
    """update_dictionaries.sh compares against this file instead of
    overwriting in place; a dictionary missing from it is unpinned."""
    lock = MmcifDictionary.DATA_DIR / "dictionaries.lock"
    assert lock.exists(), "no dictionaries.lock"

    pinned = {}
    for line in lock.read_text().splitlines():
        if line.startswith("#") or not line.strip():
            continue
        name, version, digest = line.split()
        pinned[name] = (version, digest)

    upstream = [d for d in MmcifDictionary.BUNDLED_DICTS if not d.startswith("mmfdb_")]
    assert set(upstream) <= set(pinned), (
        f"unpinned upstream dictionaries: {sorted(set(upstream) - set(pinned))}"
    )
    for name in upstream:
        version, digest = pinned[name]
        assert dic.dictionary_version(name) == version, f"{name}: lock disagrees on version"
        assert dic.dictionary_hash(name) == digest, f"{name}: lock disagrees on content"


# -- a written row says which vocabulary it meant ------------------------------


def test_an_operation_records_the_dictionary_it_was_tagged_under():
    from mmfdb.repository import MFDatabase

    path = tempfile.mktemp(suffix=".db")
    try:
        db = MFDatabase(path)
        db.record_operation(operation_id="op-1", operation_type="burst_fusion")
        row = db.get_operation("op-1")
        assert row["dictionary_version"] == extension_dictionary_version()
        assert row["dictionary_hash"] == extension_dictionary_hash()
        assert len(row["dictionary_hash"]) == 64
    finally:
        Path(path).unlink(missing_ok=True)


def test_a_database_stamped_behind_gains_the_new_vocabulary():
    """Migration 45 had to re-seed the vocabulary for one added term; any
    dictionary revision that adds a term needs the same, because artifact_kind
    is validated under the closed policy and bootstrap runs on a fresh
    database only."""
    path = tempfile.mktemp(suffix=".db")
    conn = sqlite3.connect(path)
    try:
        schema.migrate_schema(conn)
        assert schema.get_schema_version(conn) == schema.SCHEMA_VERSION

        conn.execute("DELETE FROM mmfdb_vocabulary WHERE value IN ('pixel_map', 'burst_fusion')")
        schema.set_schema_version(conn, 47)

        schema.migrate_schema(conn)
        seeded = {r[0] for r in conn.execute("SELECT value FROM mmfdb_vocabulary")}
        assert {"pixel_map", "burst_fusion"} <= seeded
        columns = {r[1] for r in conn.execute("PRAGMA table_info(mmfdb_operation)")}
        assert {"dictionary_version", "dictionary_hash"} <= columns
    finally:
        conn.close()
        Path(path).unlink(missing_ok=True)


# -- the terms PTO.CS needs ----------------------------------------------------


@pytest.mark.parametrize(
    "item, terms",
    [
        ("_mmfdb_artifact.data_format", {"pto", "dstore", "tiff", "npy"}),
        (
            "_mmfdb_artifact.artifact_kind",
            {"pixel_map", "velocity_field", "track_table", "dwell_table"},
        ),
        (
            "_mmfdb_operation.operation_type",
            {"burst_fusion", "photon_hmm", "burst_variance_analysis"},
        ),
    ],
)
def test_the_container_profile_has_vocabulary_to_name_what_it_writes(dic, item, terms):
    """PTO.CS defines no terms of its own: a kind, an encoding and a step are
    all values of these enumerations. A term missing here is a writer that
    would have to invent one."""
    assert terms <= set(dic.get_enumerations(item)), (
        f"{item} is missing {sorted(terms - set(dic.get_enumerations(item)))}"
    )


def test_the_integrity_items_are_declared(dic):
    """The columns existed in hand-written DDL long before any dictionary item
    described them, so the vocabulary for 'restorable with a guarantee' was
    undeclared even though the storage was there."""
    for name in (
        "_mmfdb_artifact.checksum",
        "_mmfdb_artifact.checksum_algorithm",
        "_mmfdb_artifact.size_bytes",
        "_mmfdb_artifact.row_count",
        "_mmfdb_artifact.mime_type",
        "_mmfdb_artifact.storage_mode",
        "_mmfdb_artifact.file_path",
    ):
        assert dic.get_item(name) is not None, f"{name} is not declared"
    assert set(dic.get_enumerations("_mmfdb_artifact.checksum_algorithm")) == {"sha256", "md5"}
