"""Back-compat for the vendor-neutral namespace rename (PRD-44).

Two namespaces were de-branded from the consuming application to the store.

* The dictionary's schema-mapping **tags**, ``_chisurf_schema.*`` ->
  ``_mmfdb_schema.*``. The parser keeps a legacy-fallback branch so old ``.dic``
  copies (and any third-party dictionaries) using the branded tag still parse.
* The exporter's local extension **categories** in flrCIF output,
  ``_chisurf_probe_spectrum`` and its four siblings -> ``_mmfdb_*``. The
  importer accepts both, so files written before the rename still read.

This guards both fallbacks, and pins that nothing writes the branded spelling
any more.
"""

from __future__ import annotations

import ast
import tempfile
from pathlib import Path

import mmfdb.repository as repository_module
from mmfdb.cif_writer import (
    EXTENSION_CATEGORIES,
    EXTENSION_CATEGORY_ALIASES,
    canonical_extension_category,
)
from mmfdb.schema.pdbx_metadata import MmcifDictionary

_LEGACY_FRAGMENT = """\
data_legacy_test

save__test_legacy_cat.value
   _item.name                "_test_legacy_cat.value"
   _item.category_id         test_legacy_cat
   _item_type.code           float
   _chisurf_schema.table_name  test_legacy_table
   _chisurf_schema.column_name legacy_value
   _chisurf_schema.status      active
   _item_description.description
;     legacy schema-mapped value
;

save__test_legacy_cat.parent_id
   _item.name                "_test_legacy_cat.parent_id"
   _item.category_id         test_legacy_cat
   _item_type.code           int
   _chisurf_schema.table_name  test_legacy_table
   _chisurf_schema.column_name parent_id
   _chisurf_schema.foreign_key parent_table(parent_id)
   _item_description.description
;     legacy schema-mapped foreign key
;
"""


def _parse_fragment(text: str) -> MmcifDictionary:
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "legacy.dic"
        p.write_text(text, encoding="utf-8")
        return MmcifDictionary(p)


def test_legacy_chisurf_schema_tags_still_parse() -> None:
    dic = _parse_fragment(_LEGACY_FRAGMENT)

    value = dic.get_item("_test_legacy_cat.value")
    assert value is not None, "legacy item not parsed"
    assert value.schema_table == "test_legacy_table"
    assert value.schema_column == "legacy_value"
    assert value.schema_status == "active"

    fk = dic.get_item("_test_legacy_cat.parent_id")
    assert fk is not None
    assert fk.schema_foreign_key == "parent_table(parent_id)"


def test_new_mmfdb_schema_tags_parse_identically() -> None:
    """The new spelling populates the same fields (parity with the legacy path)."""
    new_fragment = _LEGACY_FRAGMENT.replace("_chisurf_schema", "_mmfdb_schema")
    dic = _parse_fragment(new_fragment)

    value = dic.get_item("_test_legacy_cat.value")
    assert value is not None
    assert value.schema_table == "test_legacy_table"
    assert value.schema_column == "legacy_value"
    assert value.schema_status == "active"


def test_extension_category_alias_table_is_a_pure_debranding() -> None:
    """Every alias removes a program name and only that.

    The original five were ``_chisurf_*`` -> ``_mmfdb_*`` prefix swaps;
    ``_flr_chisurf_parameter`` -> ``_flr_fit_parameter`` (2026-09-03)
    de-brands inside the flr namespace instead. The invariant both share:
    the legacy spelling names a program, the canonical one does not.
    """
    assert EXTENSION_CATEGORY_ALIASES, "the alias table must not be empty"
    for legacy, canonical in EXTENSION_CATEGORY_ALIASES.items():
        assert "chisurf" in legacy, legacy
        assert "chisurf" not in canonical, canonical
        assert canonical.startswith(("_mmfdb_", "_flr_")), canonical
    assert len(EXTENSION_CATEGORIES) == len(EXTENSION_CATEGORY_ALIASES)


def test_legacy_extension_categories_map_onto_the_canonical_spelling() -> None:
    assert canonical_extension_category("_chisurf_probe_spectrum") == "_mmfdb_probe_spectrum"
    for legacy, canonical in EXTENSION_CATEGORY_ALIASES.items():
        assert canonical_extension_category(legacy) == canonical
        assert canonical_extension_category(canonical) == canonical


def test_standard_categories_pass_through_untouched() -> None:
    """Only the local extension namespace is rewritten, never a standard one."""
    for name in ("_flr_sample", "_struct_ref", "_entity", "_flr_fret_forster_radius"):
        assert canonical_extension_category(name) == name


def _string_constants(path: Path) -> set[str]:
    """Every string literal in *path*, so a rename guard ignores prose."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }


def test_the_exporter_writes_no_branded_extension_category() -> None:
    """The rename is only done if nothing emits the legacy spelling any more."""
    branded = {
        text
        for text in _string_constants(Path(repository_module.__file__))
        if text.startswith("_chisurf_")
    }
    assert not branded, f"exporter still writes branded categories: {sorted(branded)}"
