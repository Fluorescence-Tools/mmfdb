"""Every extension category and item carries a description.

The ``.dic`` files are what a deposited file is read against, so an
undescribed term is an undefined term. Categories are held to the full rule.
Items are held to it too, except for the names in the **shrinking**
``dictionary_item_description_baseline.txt``: a name there is a debt, never
somewhere to add yourself. The test also fails when a baseline name has been
documented, so the file only ever gets shorter.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mmfdb.schema.pdbx_metadata import MmcifDictionary

EXTENSIONS = ["mmfdb_flr_ext.dic", "mmfdb_workflow_ext.dic"]
BASELINE = Path(__file__).with_name("dictionary_item_description_baseline.txt")


@pytest.fixture(scope="module")
def extension() -> MmcifDictionary:
    return MmcifDictionary(*[MmcifDictionary.DATA_DIR / n for n in EXTENSIONS])


def _baseline() -> set[str]:
    return {
        line.strip()
        for line in BASELINE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }


def test_every_mmfdb_category_has_a_description(extension: MmcifDictionary) -> None:
    missing = [
        n for n, c in extension._categories.items()
        if n.startswith("mmfdb_") and not c.description.strip()
    ]
    assert not missing, f"categories without _category.description: {missing}"


def test_every_mmfdb_item_has_a_description_or_is_baselined(
    extension: MmcifDictionary,
) -> None:
    undocumented = {
        n for n, i in extension._items.items()
        if n.startswith("_mmfdb_") and not i.description.strip()
    }
    new = sorted(undocumented - _baseline())
    assert not new, f"items without _item_description.description: {new}"
    fixed = sorted(_baseline() - undocumented)
    assert not fixed, f"documented now; strike from the baseline: {fixed}"
