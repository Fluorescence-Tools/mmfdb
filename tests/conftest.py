"""Hermetic test harness for the standalone MMFDB package.

These tests import only :mod:`mmfdb` — never ChiSurf. Isolation is achieved
through MMFDB's own runtime config: ``MMFDB_SETTINGS_DIR`` redirects the per-user
state directory (sample database + object store) to a throwaway location for the
whole session, so no test can read or write the real ``~/.chisurf``.

Run standalone from the package root:

    PYTHONPATH=src pytest            # or: pixi run test-mmfdb
"""

from __future__ import annotations

import os
import pathlib
import tempfile

import pytest

# Redirect MMFDB state *before* any mmfdb import resolves a path.
_REAL_SETTINGS_DIR = pathlib.Path.home() / ".chisurf"
_HERMETIC_SETTINGS_DIR = pathlib.Path(tempfile.mkdtemp(prefix="mmfdb-test-settings-"))
os.environ["MMFDB_SETTINGS_DIR"] = str(_HERMETIC_SETTINGS_DIR)


@pytest.fixture(scope="session", autouse=True)
def _hermetic_settings_dir():
    """Point MMFDB at the temp settings dir and seed a fresh, current-schema DB.

    Pre-creating the user database means ``resolve_database_path`` finds it and
    does not copy any shipped curated source DB (older schema / demo data).
    """
    os.environ["MMFDB_SETTINGS_DIR"] = str(_HERMETIC_SETTINGS_DIR)
    try:
        from mmfdb.store.database_resolver import user_database_path
        from mmfdb.repository import MFDatabase

        user_db = user_database_path()
        user_db.parent.mkdir(parents=True, exist_ok=True)
        if not user_db.exists():
            MFDatabase(str(user_db)).close()
    except Exception:
        pass
    yield _HERMETIC_SETTINGS_DIR


def _find_host_manifest() -> pathlib.Path | None:
    """Locate the host application's mmfdb-admin plugin manifest, if present.

    The RPC surface MMFDB registers is *declared* in the host's plugin manifest;
    this package ships no copy of it. The manifest is only read, never imported,
    so the "tests import only mmfdb" rule holds.

    Resolution order: an explicit ``MMFDB_HOST_MANIFEST`` path, then a search up
    the directory tree for a host checkout — either an ancestor that *is* the
    host repository, or an ancestor holding it beside this one. Both the symlink
    -resolved and unresolved paths are walked, because this package is commonly
    symlinked into the host repository as a submodule directory.

    Returns
    -------
    pathlib.Path or None
        The manifest path, or ``None`` when no host checkout is reachable.
    """
    override = os.environ.get("MMFDB_HOST_MANIFEST")
    if override:
        path = pathlib.Path(override)
        return path if path.is_file() else None

    tail = pathlib.Path("chisurf") / "plugins" / "core" / "mmfdb_admin" / "manifest.json"
    here = pathlib.Path(__file__).absolute()
    for start in (here, here.resolve()):
        for ancestor in start.parents:
            for root in (ancestor, ancestor / "chisurf"):
                candidate = root / tail
                if candidate.is_file():
                    return candidate
    return None


@pytest.fixture(scope="session")
def host_manifest_path() -> pathlib.Path:
    """Return the host mmfdb-admin manifest, skipping when it is unreachable."""
    path = _find_host_manifest()
    if path is None:
        pytest.skip("host plugin manifest not present (standalone mmfdb checkout)")
    return path


@pytest.fixture(autouse=True)
def _guard_real_user_db():
    """Fail loudly if a test resolves MMFDB state to the real ~/.chisurf."""
    from mmfdb.store.database_resolver import object_store_root, user_database_path

    real = _REAL_SETTINGS_DIR.resolve()
    assert real not in user_database_path().resolve().parents, (
        f"Test would use the REAL user database at {user_database_path()}."
    )
    assert (
        real not in object_store_root().resolve().parents
        and object_store_root().resolve() != real
    ), f"Test would use the REAL object store at {object_store_root()}."
    yield


@pytest.fixture(autouse=True)
def _reset_global_db():
    """Reset the process-global active DB between tests.

    ``result_registry`` caches the active database in a module global; a test
    that sets it must not leak that connection into later tests.
    """
    try:
        from mmfdb.provenance.result_registry import set_global_db
    except Exception:
        set_global_db = None
    if set_global_db is not None:
        set_global_db(None)
    yield
    if set_global_db is not None:
        set_global_db(None)
