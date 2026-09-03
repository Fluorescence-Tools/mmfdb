"""Dictionary keywords never name a program (rule, 2026-09-03).

The mmfdb/flrCIF dictionaries are the central, software-agnostic
vocabulary every producing application maps onto. A category or item
named after one program (``flr_chisurf_parameter`` was the offender)
brands the shared schema with a single consumer and breaks the promise
that two programs exporting the same quantity use the same keyword.
Keywords are scanned here; enumeration *values* and prose descriptions
may mention software (a provenance record legitimately names the tool
that ran), keywords may not. The legacy ``_chisurf_schema.*`` tag
fallback (PRD-44) parses old third-party dictionaries and is exercised
elsewhere; the bundled dictionaries themselves must be clean.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_BRANDS = ("chisurf", "chinet", "ndxplorer", "tttrlib", "quickfit",
           "chimol", "fretbursts")

_DATA = Path(__file__).resolve().parent.parent / "src" / "mmfdb" / "data"


@pytest.mark.parametrize("dic", sorted(_DATA.glob("*.dic")),
                         ids=lambda p: p.name)
def test_no_branded_category_or_item_names(dic):
    text = dic.read_text(encoding="utf-8", errors="replace")
    keywords = re.findall(r"^save_(\S+)", text, re.M)
    keywords += re.findall(r'^\s*_item\.name\s+"([^"]+)"', text, re.M)
    keywords += re.findall(r"^\s*_category\.id\s+(\S+)", text, re.M)
    branded = sorted({
        k for k in keywords
        if any(b in k.lower() for b in _BRANDS)
    })
    assert not branded, (
        f"{dic.name}: keywords naming a program (rename to an agnostic "
        f"term, alias the old spelling in cif_writer for old files): "
        f"{branded[:10]}")
