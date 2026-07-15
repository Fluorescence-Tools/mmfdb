"""Setuptools hooks for reproducible MMFDB wheels."""

from __future__ import annotations

from pathlib import Path
from shutil import rmtree

from setuptools import setup
from setuptools.command.build_py import build_py


class IsolatedBuildPy(build_py):
    """Remove obsolete pre-rename package output from incremental builds."""

    def run(self) -> None:
        """Build configured packages, then discard any legacy build output."""
        super().run()
        legacy_package = Path(self.build_lib) / "mfdb"
        if legacy_package.exists():
            rmtree(legacy_package)


setup(cmdclass={"build_py": IsolatedBuildPy})
