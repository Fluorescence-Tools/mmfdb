"""Run the YAML-configured standalone MMFDB HTTP server."""

from __future__ import annotations

import sys

from mmfdb.cli import cli


def main() -> None:
    """Route legacy module execution through validated init + serve startup."""
    cli.main(
        args=["serve", *sys.argv[1:]],
        prog_name="python -m mmfdb.webadmin",
    )


if __name__ == "__main__":
    main()
