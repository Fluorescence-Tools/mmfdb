#!/usr/bin/env python3
"""population_split.py — split a distribution into low/high populations.

This tool is used **two ways** by the example workflows, to show that a single
piece of software can be stitched in as either a subprocess or an in-process
Python step:

* as a **command-line** step (``cmd:``) via :func:`main`, and
* as a **Python** step (``python: tools/population_split.py:run``) via
  :func:`run`, which receives a workflow ``StepContext``.

Both split ``proximity_ratio`` at a threshold and report the fraction and mean
of each population — a stand-in for a two-state FRET analysis, kept to the
standard library so the examples run anywhere.
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
from pathlib import Path


def split(values: list[float], threshold: float) -> dict[str, object]:
    """Return per-population fractions and means for a threshold split."""
    low = [v for v in values if v < threshold]
    high = [v for v in values if v >= threshold]
    total = len(values) or 1
    return {
        "n": len(values),
        "threshold": threshold,
        "low_fraction": round(len(low) / total, 4),
        "high_fraction": round(len(high) / total, 4),
        "low_mean": round(statistics.fmean(low), 6) if low else None,
        "high_mean": round(statistics.fmean(high), 6) if high else None,
    }


def _read_column(path: str | Path, column: str) -> list[float]:
    with open(path, newline="", encoding="utf-8") as handle:
        return [float(row[column]) for row in csv.DictReader(handle)]


def _write(summary: dict[str, object], path: str | Path) -> None:
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["population", "fraction", "mean"])
        writer.writerow(["low", summary["low_fraction"], summary["low_mean"]])
        writer.writerow(["high", summary["high_fraction"], summary["high_mean"]])


def run(ctx) -> dict[str, object]:
    """Workflow Python step: split ``inputs['table']`` into ``outputs['populations']``."""
    column = ctx.params.get("column", "proximity_ratio")
    threshold = float(ctx.params.get("threshold", 0.5))
    values = _read_column(ctx.inputs["table"], column)
    summary = split(values, threshold)
    _write(summary, ctx.outputs["populations"])
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input")
    parser.add_argument("--column", default="proximity_ratio")
    parser.add_argument("--threshold", type=float, default=0.5)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    summary = split(_read_column(args.input, args.column), args.threshold)
    _write(summary, args.output)
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
