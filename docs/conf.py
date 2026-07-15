"""Sphinx configuration for the standalone MMFDB documentation.

MMFDB ships its own docs so they travel with the package when it becomes its own
repository. Build with::

    sphinx-build -b html docs docs/_build
"""

from __future__ import annotations

project = "MMFDB"
author = "Thomas-Otavio Peulen"
copyright = "2026, Thomas-Otavio Peulen"

extensions = [
    "myst_nb",  # Markdown (MyST) sources + renders example .ipynb as pages
]

myst_enable_extensions = [
    "colon_fence",
    "deflist",
]

# Example notebooks already carry their executed outputs (they are run by the
# test suite), so the docs render the stored results without re-executing.
nb_execution_mode = "off"

source_suffix = {".md": "myst-nb", ".ipynb": "myst-nb"}
root_doc = "index"
exclude_patterns = ["_build", "Thumbs.db", ".DS_Store", "examples/external_tools"]

html_theme = "furo"
html_title = "MMFDB"

# Relative paths in {literalinclude} are resolved from the source directory.
