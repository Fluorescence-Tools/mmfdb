#!/usr/bin/env python3
"""select_bursts.py — filter a burst table by a column range (stdlib only).

A deliberately MMFDB-agnostic command-line tool for the example workflows: it
reads a CSV, keeps rows whose ``--column`` value is within ``[--min, --max]``,
writes the filtered CSV, and prints a JSON summary to stdout. It stands in for
any real burst-selection step so the example workflows run anywhere without
tttrlib or other heavy dependencies.

Example::

    python select_bursts.py bursts.csv --column proximity_ratio \\
        --min 0.4 --output selected.csv
"""

from __future__ import annotations

import argparse
import csv
import json


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input")
    parser.add_argument("--column", default="proximity_ratio")
    parser.add_argument("--min", type=float, default=float("-inf"))
    parser.add_argument("--max", type=float, default=float("inf"))
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    with open(args.input, newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        fieldnames = reader.fieldnames or []
        rows = list(reader)

    kept = [r for r in rows if args.min <= float(r[args.column]) <= args.max]
    with open(args.output, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(kept)

    print(json.dumps({"n_in": len(rows), "n_out": len(kept), "column": args.column}))


if __name__ == "__main__":
    main()
