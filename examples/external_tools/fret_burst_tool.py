#!/usr/bin/env python3
"""fret_burst_tool.py — a standalone smFRET burst-analysis CLI on vanilla tttrlib.

This is deliberately an **external tool**: it depends only on ``numpy``,
``click``, and ``tttrlib`` and knows nothing about ChiSurf or MMFDB. It reads a
TTTR file, runs a cumulative (CUSUM/SPRT) burst search, computes the per-burst
proximity ratio ``PR = n_acceptor / (n_donor + n_acceptor)``, writes a CSV, and
prints a JSON summary to stdout.

It exists so the provenance example
(``examples/mmfdb_06_external_tool_provenance.ipynb``) can drive a
real subprocess and record its exact invocation, inputs, outputs, and result in
the MMFDB provenance graph — exactly as one would wrap any third-party CLI.

Example
-------
::

    python fret_burst_tool.py measurement.spc --file-type SPC-130 \\
        --min-photons 20 --donor 0 --donor 8 --acceptor 1 --acceptor 9 \\
        --output bursts.csv
"""

from __future__ import annotations

import json

import click
import numpy as np
import tttrlib


def run(
    input_path: str,
    *,
    file_type: str = "SPC-130",
    min_photons: int = 20,
    background_cps: float = 40.0,
    sbr: float = 4.0,
    donor: tuple[int, ...] = (0,),
    acceptor: tuple[int, ...] = (1,),
    output: str = "bursts.csv",
) -> dict:
    """Run the burst search, write the CSV, and return a summary dict."""
    tttr = tttrlib.TTTR(input_path, file_type)
    start_stop = np.asarray(
        tttr.burst_search_cusum_sprt(
            min_photons=min_photons,
            background_cps=background_cps,
            signal_to_background_ratio=sbr,
            alpha=0.01,
            beta=0.01,
        )
    )
    starts, stops = start_stop[0::2], start_stop[1::2]
    routing = np.asarray(tttr.routing_channels)

    ratios = []
    for start, stop in zip(starts, stops):
        burst = routing[int(start):int(stop) + 1]
        n_d = int(np.isin(burst, donor).sum())
        n_a = int(np.isin(burst, acceptor).sum())
        if n_d + n_a > 0:
            ratios.append(n_a / (n_d + n_a))
    ratios = np.asarray(ratios, dtype=float)
    np.savetxt(output, ratios, header="proximity_ratio", comments="")

    return {
        "tool": "fret_burst_tool",
        "backend": f"tttrlib {tttrlib.__version__}",
        "input": input_path,
        "output": output,
        "n_bursts": int(ratios.size),
        "mean_proximity_ratio": float(np.mean(ratios)) if ratios.size else None,
        "params": {
            "min_photons": min_photons,
            "background_cps": background_cps,
            "signal_to_background_ratio": sbr,
            "donor": list(donor),
            "acceptor": list(acceptor),
        },
    }


@click.command()
@click.argument("input_path", type=click.Path(exists=True, dir_okay=False))
@click.option("--file-type", default="SPC-130", show_default=True, help="tttrlib file type.")
@click.option("--min-photons", type=int, default=20, show_default=True,
              help="Min photons per burst.")
@click.option("--background-cps", type=float, default=40.0, show_default=True,
              help="Background count rate.")
@click.option("--sbr", type=float, default=4.0, show_default=True,
              help="Signal-to-background ratio.")
@click.option("--donor", type=int, multiple=True, default=(0,), show_default=True,
              help="Donor channel (repeatable).")
@click.option("--acceptor", type=int, multiple=True, default=(1,), show_default=True,
              help="Acceptor channel (repeatable).")
@click.option("--output", type=click.Path(dir_okay=False), default="bursts.csv",
              show_default=True, help="Output CSV path.")
def main(
    input_path: str,
    file_type: str,
    min_photons: int,
    background_cps: float,
    sbr: float,
    donor: tuple[int, ...],
    acceptor: tuple[int, ...],
    output: str,
) -> None:
    """smFRET burst analysis (vanilla tttrlib): print a JSON summary to stdout."""
    summary = run(
        input_path,
        file_type=file_type,
        min_photons=min_photons,
        background_cps=background_cps,
        sbr=sbr,
        donor=donor,
        acceptor=acceptor,
        output=output,
    )
    click.echo(json.dumps(summary))


if __name__ == "__main__":
    main()
