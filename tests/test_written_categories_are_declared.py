"""Every category/item name a writer emits is declared in the dictionaries.

The ``.dic`` files are the schema authority. A writer that emits a name
the dictionaries do not define produces a file ``python-ihm`` reports as an
``UnknownCategoryWarning`` and that no reader can resolve. The fix is an item
in ``mmfdb_flr_ext.dic`` / ``mmfdb_workflow_ext.dic``, never a rename in a
plugin.

Static scan: every ``"_mmfdb_*.item"`` / ``"_flr_*.item"`` / ``"_ihm_*.item"``
string literal in mmfdb's sources and in ChiSurf's ``core/fio`` (the
``.pto`` metadata writer/reader, found via ``CHISURF_ROOT`` or the sibling
checkout) must resolve. The internal ``*_schema`` bookkeeping categories are
deliberately not database vocabulary and are exempt.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from mmfdb.schema.pdbx_metadata import MmcifDictionary

SRC = Path(__file__).resolve().parents[1] / "src" / "mmfdb"
_ITEM = re.compile(r"""["'](_(?:mmfdb|chisurf|flr|ihm)_[a-z0-9_]+\.[a-z0-9_]+)["']""")
_EXEMPT = re.compile(r"^_(?:mmfdb|chisurf)_schema\.")


@pytest.fixture(scope="module")
def dictionary() -> MmcifDictionary:
    data = MmcifDictionary.DATA_DIR
    names = MmcifDictionary.BUNDLED_DICTS + MmcifDictionary.EXPORT_ONLY_DICTS
    return MmcifDictionary(*[data / n for n in names])


def _roots() -> list[Path]:
    roots = [SRC]
    chisurf = os.environ.get("CHISURF_ROOT")
    candidates = [Path(chisurf)] if chisurf else [Path(__file__).resolve().parents[2] / "chisurf"]
    for c in candidates:
        fio = c / "chisurf" / "core" / "fio"
        if fio.is_dir():
            roots.append(fio)
    return roots


def test_every_written_item_is_declared(dictionary: MmcifDictionary) -> None:
    undeclared: dict[str, set[str]] = {}
    for root in _roots():
        for path in root.rglob("*.py"):
            for match in _ITEM.finditer(path.read_text(encoding="utf-8")):
                name = match.group(1)
                if _EXEMPT.match(name) or dictionary.get_item(name) is not None:
                    continue
                undeclared.setdefault(name, set()).add(path.name)
    assert not undeclared, (
        "names written/read but not defined in the bundled dictionaries "
        f"(add them to mmfdb_flr_ext.dic / mmfdb_workflow_ext.dic): {undeclared}"
    )


def test_scan_covers_the_pto_writer() -> None:
    names = {p.name for r in _roots() for p in r.rglob("*.py")}
    assert "cif_writer.py" in names
