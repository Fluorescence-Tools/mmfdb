"""Small, dependency-free CIF writer for MMFDB tabular exports.

MMFDB only emits data blocks and loops.  Keeping that narrow writer here avoids
making the entire database runtime depend on the much larger python-ihm model
stack while still producing standards-compliant CIF text.
"""

from __future__ import annotations

import math
from contextlib import AbstractContextManager
from typing import Any, Literal, TextIO


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
