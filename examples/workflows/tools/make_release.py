#!/usr/bin/env python3
"""make_release.py — assemble a set of result tables into a release manifest.

Given several member CSVs (the *set* being published), this tool writes a
manifest listing each member with its row count and a content hash, and prints a
JSON summary. It is a **multi-input** step: because the manifest operation links
every member as an input artifact, seeding a deposition bundle at the manifest
captures the whole set (all member files, the provenance graph, and metadata) in
one publication ZIP.

Example::

    python make_release.py rep1.csv rep2.csv pooled.csv --output manifest.csv
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os


def _row_count(path: str) -> int:
    with open(path, newline="", encoding="utf-8") as handle:
        return sum(1 for _ in csv.reader(handle)) - 1  # minus the header


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="+", help="Member CSVs to include in the release.")
    parser.add_argument("--output", required=True, help="Manifest CSV path.")
    args = parser.parse_args()

    total = 0
    with open(args.output, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["member", "n_rows", "sha256"])
        for path in args.inputs:
            rows = _row_count(path)
            total += rows
            writer.writerow([os.path.basename(path), rows, _sha256(path)])

    print(json.dumps({"n_members": len(args.inputs), "total_rows": total}))


if __name__ == "__main__":
    main()
