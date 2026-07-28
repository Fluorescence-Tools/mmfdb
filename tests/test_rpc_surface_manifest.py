"""The registered RPC surface and the host manifest must agree, method for method.

Only the 40 versioned ``mmfdb.v1.*`` methods used to be checked, leaving the
other ~180 handlers free to drift from the manifest without anything noticing:
an undeclared handler is invisible to the clients that build their forms and
documentation from the manifest, and a declared method with no handler is an
advertised call that fails at dispatch.
"""

from __future__ import annotations

import json
import pathlib

from mmfdb.admin.backend.services import (
    VERSIONED_MMFDB_METHODS,
    _validate_mmfdb_methods_in_manifest,
    registered_service_names,
    validate_manifest_rpc_surface,
)


def test_registered_surface_covers_both_families() -> None:
    """Enumeration needs no database and yields both method families."""
    names = registered_service_names()
    assert len(names) > len(VERSIONED_MMFDB_METHODS)
    assert "mmfdb.v1.artifacts.register" in names
    assert "mmfdb.status" in names
    # Registered only when the host supplies the optional runners; the manifest
    # declares them regardless, so enumeration must include them.
    assert "processing.burst_selection.run" in names
    assert "mmfdb.pipelines.list" in names
    assert "fluorophores.ai_triage" in names


def test_manifest_declares_exactly_the_registered_surface(
    host_manifest_path: pathlib.Path,
) -> None:
    """No handler is undeclared and no declared method lacks a handler."""
    drift = validate_manifest_rpc_surface(host_manifest_path)
    assert drift["undeclared"] == [], (
        f"registered but missing from the manifest: {drift['undeclared']}"
    )
    assert drift["unregistered"] == [], (
        f"declared in the manifest but never registered: {drift['unregistered']}"
    )


def test_drift_in_both_directions_is_reported(tmp_path: pathlib.Path) -> None:
    """A manifest that omits real methods and invents one is caught both ways."""
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps({"rpc_methods": [{"name": "mmfdb.status"}, {"name": "mmfdb.invented"}]}),
        encoding="utf-8",
    )
    drift = validate_manifest_rpc_surface(manifest_path)
    assert "mmfdb.v1.artifacts.register" in drift["undeclared"]
    assert "mmfdb.status" not in drift["undeclared"]
    assert drift["unregistered"] == ["mmfdb.invented"]


def test_versioned_check_still_reports_only_the_v1_family(tmp_path: pathlib.Path) -> None:
    """The narrower legacy check keeps its meaning and now needs an explicit path."""
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps({"rpc_methods": []}), encoding="utf-8")
    missing = _validate_mmfdb_methods_in_manifest(manifest_path)
    assert set(missing) == {f"mmfdb.v1.{name}" for name in VERSIONED_MMFDB_METHODS}
