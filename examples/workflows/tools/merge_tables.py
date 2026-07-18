#!/usr/bin/env python3
"""merge_tables.py — concatenate two CSV tables with the same header (stdlib only).

Used by the fan-in example to show a **multi-input** workflow step: two upstream
tables (e.g. two experimental replicates) are merged into one, which the
provenance graph records as a single operation with two input artifacts. Writes
the merged CSV and prints a JSON summary.

Example::

    python merge_tables.py rep1.csv rep2.csv --output merged.csv
"""

from __future__ import annotations

import argparse
import csv
import json


def _read(path: str) -> tuple[list[str], list[dict[str, str]]]:
    with open(path, newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        return (reader.fieldnames or []), list(reader)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="+", help="Two or more CSV files to concatenate.")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    fieldnames: list[str] = []
    merged: list[dict[str, str]] = []
    per_file: list[int] = []
    for path in args.inputs:
        names, rows = _read(path)
        if not fieldnames:
            fieldnames = names
        elif names != fieldnames:
            raise SystemExit(f"header mismatch in {path}: {names} != {fieldnames}")
        merged.extend(rows)
        per_file.append(len(rows))

    with open(args.output, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(merged)

    print(json.dumps({"n_files": len(args.inputs), "n_total": len(merged), "per_file": per_file}))


if __name__ == "__main__":
    main()
