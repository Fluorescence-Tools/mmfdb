#!/usr/bin/env python3
"""histogram.py — bin a numeric CSV column into a histogram (stdlib only).

Reads a CSV, bins ``--column`` into ``--bins`` equal-width bins between the
observed minimum and maximum, writes a ``bin_center,count`` CSV, and prints a
JSON summary. Used by the example workflows as a generic downstream step.

Example::

    python histogram.py selected.csv --column proximity_ratio \\
        --bins 20 --output hist.csv
"""

from __future__ import annotations

import argparse
import csv
import json


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input")
    parser.add_argument("--column", default="proximity_ratio")
    parser.add_argument("--bins", type=int, default=20)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    with open(args.input, newline="", encoding="utf-8") as handle:
        values = [float(row[args.column]) for row in csv.DictReader(handle)]

    if not values:
        raise SystemExit(f"no data in column {args.column!r}")

    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0
    width = span / args.bins
    counts = [0] * args.bins
    for value in values:
        index = min(int((value - lo) / width), args.bins - 1)
        counts[index] += 1

    with open(args.output, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["bin_center", "count"])
        for i, count in enumerate(counts):
            writer.writerow([round(lo + (i + 0.5) * width, 6), count])

    print(json.dumps({"n": len(values), "bins": args.bins, "min": lo, "max": hi}))


if __name__ == "__main__":
    main()
