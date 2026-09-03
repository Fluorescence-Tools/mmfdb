"""Small, dependency-free CIF writer for MMFDB tabular exports.

MMFDB only emits data blocks and loops.  Keeping that narrow writer here avoids
making the entire database runtime depend on the much larger python-ihm model
stack while still producing standards-compliant CIF text.
"""

from __future__ import annotations

import math
from contextlib import AbstractContextManager
from typing import Any, Literal, TextIO

#: Legacy spelling -> canonical spelling for MMFDB's own flrCIF extension
#: categories.  These carry the data no standard mmCIF/flrCIF category has a
#: home for (probe spectra, optical properties, photon streams, analysis
#: curves), so they live in a local extension namespace.  That namespace is
#: keyed on the **store** (``mmfdb``) rather than on a consuming application,
#: the same de-branding PRD-44 applied to the dictionary's schema-mapping tags;
#: files written before the rename carry the ``_chisurf_*`` spelling and are
#: still read.
EXTENSION_CATEGORY_ALIASES: dict[str, str] = {
    "_chisurf_analysis_metadata": "_mmfdb_analysis_metadata",
    "_chisurf_probe_property": "_mmfdb_probe_property",
    "_chisurf_probe_spectrum": "_mmfdb_probe_spectrum",
    "_chisurf_photon_stream": "_mmfdb_photon_stream",
    "_chisurf_analysis_data": "_mmfdb_analysis_data",
    # The fitted-parameter category missed by PRD-44's pass, de-branded
    # 2026-09-03 (dictionary keywords are software-agnostic, by rule).
    "_flr_chisurf_parameter": "_flr_fit_parameter",
}

#: The canonical extension categories an MMFDB export writes.
EXTENSION_CATEGORIES: frozenset[str] = frozenset(EXTENSION_CATEGORY_ALIASES.values())


def canonical_extension_category(name: str) -> str:
    """Map an extension category name onto its canonical spelling.

    Parameters
    ----------
    name : str
        A CIF category name including its leading underscore, e.g.
        ``"_chisurf_probe_spectrum"``.

    Returns
    -------
    str
        The canonical ``_mmfdb_*`` spelling if *name* is a known legacy
        extension category, otherwise *name* unchanged — standard mmCIF
        categories pass through untouched.

    Examples
    --------
    >>> canonical_extension_category("_chisurf_probe_spectrum")
    '_mmfdb_probe_spectrum'
    >>> canonical_extension_category("_flr_sample")
    '_flr_sample'
    """
    return EXTENSION_CATEGORY_ALIASES.get(name, name)


def _format_value(value: Any) -> str:
    """Serialize one scalar as a CIF token."""
    if value is None:
        return "."
    if isinstance(value, bool):
        return "YES" if value else "NO"
    if isinstance(value, float) and not math.isfinite(value):
        return "."
    if isinstance(value, (int, float)):
        return str(value)

    text = str(value)
    if not text:
        return "''"
    if "\n" in text:
        return f";{text}\n;"
    needs_quotes = (
        any(char.isspace() for char in text)
        or text[0] in "_#$;'\""
        or text.lower().startswith(("data_", "loop_", "save_", "stop_", "global_"))
        or text in {".", "?"}
    )
    if not needs_quotes:
        return text
    if "'" not in text:
        return f"'{text}'"
    if '"' not in text:
        return f'"{text}"'
    return f";{text}\n;"


class _Loop(AbstractContextManager["_Loop"]):
    def __init__(self, output: TextIO, category: str, columns: list[str]) -> None:
        self._output = output
        self._category = category.rstrip(".")
        self._columns = tuple(columns)
        self._rows: list[tuple[str, ...]] = []

    def __enter__(self) -> _Loop:
        return self

    def write(self, **values: Any) -> None:
        tokens = [_format_value(values.get(column)) for column in self._columns]
        # One token per physical line keeps semicolon-delimited multiline values
        # valid regardless of which loop column contains them.
        self._rows.append(tuple(tokens))

    def __exit__(self, exc_type, exc_value, traceback) -> Literal[False]:
        if exc_type is None and self._rows:
            self._output.write("loop_\n")
            for column in self._columns:
                self._output.write(f"{self._category}.{column}\n")
            for row in self._rows:
                separator = "\n" if any("\n" in token for token in row) else " "
                self._output.write(separator.join(row))
                self._output.write("\n")
            self._output.write("#\n")
        return False


class CifWriter:
    """Writer implementing the block/loop subset used by MMFDB exports."""

    def __init__(self, output: TextIO) -> None:
        self._output = output

    def start_block(self, name: str) -> None:
        safe_name = str(name).strip().replace(" ", "_")
        if not safe_name or any(char.isspace() for char in safe_name):
            raise ValueError("CIF data-block name must be non-empty and whitespace-free")
        self._output.write(f"data_{safe_name}\n#\n")

    def loop(self, category: str, columns: list[str]) -> _Loop:
        if not category.startswith("_"):
            raise ValueError("CIF category names must start with '_'")
        if not columns:
            raise ValueError("CIF loops require at least one column")
        return _Loop(self._output, category, columns)
