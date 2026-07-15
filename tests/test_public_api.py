"""Regression tests for the deliberately small root-package API."""

from __future__ import annotations

import json
import subprocess
import sys


def test_root_package_has_deliberate_public_api() -> None:
    import mmfdb

    assert mmfdb.__all__ == [
        "MFDatabase",
        "DeploymentConfig",
        "RuntimeConfig",
        "apply_deployment_config",
        "configure_runtime",
        "get_runtime_config",
        "load_client_config",
        "load_deployment_config",
        "reset_runtime_config",
    ]


def test_root_import_does_not_load_feature_modules() -> None:
    script = """
import json
import sys
import mmfdb

print(json.dumps({
    "chinet": "mmfdb.adapters.chinet" in sys.modules,
    "admin": "mmfdb.admin" in sys.modules,
    "models": "mmfdb.models" in sys.modules,
}))
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(result.stdout) == {
        "chinet": False,
        "admin": False,
        "models": False,
    }


def test_database_facade_remains_available_from_root() -> None:
    from mmfdb import MFDatabase
    from mmfdb.repository import MFDatabase as RepositoryMFDatabase

    assert MFDatabase is RepositoryMFDatabase
