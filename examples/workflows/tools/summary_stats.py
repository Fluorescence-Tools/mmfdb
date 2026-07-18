#!/usr/bin/env python3
"""summary_stats.py — summary statistics of a numeric CSV column (stdlib only).

Reads a CSV, computes count / mean / std / min / max of ``--column``, writes a
one-row ``statistic,value`` CSV, and prints the same as a JSON summary. Serves
as a terminal reporting step in the example workflows.

Example::

    python summary_stats.py bursts.csv --column proximity_ratio --output stats.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input")
    parser.add_argument("--column", default="proximity_ratio")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    with open(args.input, newline="", encoding="utf-8") as handle:
        values = [float(row[args.column]) for row in csv.DictReader(handle)]

    if not values:
        raise SystemExit(f"no data in column {args.column!r}")

    stats = {
        "n": len(values),
        "mean": round(statistics.fmean(values), 6),
        "std": round(statistics.pstdev(values), 6),
        "min": min(values),
        "max": max(values),
    }
    with open(args.output, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["statistic", "value"])
        for key, value in stats.items():
            writer.writerow([key, value])

    print(json.dumps(stats))


if __name__ == "__main__":
    main()
