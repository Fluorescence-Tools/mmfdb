"""mmCIF export through the public API boundary and the ``mmfdb`` CLI (DATA-06).

Record export used to be reachable only through the admin-only RPC handler, so
a standalone consumer had to drive :class:`~mmfdb.repository.MFDatabase`
directly. These tests pin the authenticated ``api.export_cif`` seam and the
``mmfdb export cif`` command that wraps it.
"""

from __future__ import annotations

from pathlib import Path

import mmfdb.api as api
import numpy as np
import pytest
from click.testing import CliRunner
from mmfdb.admin.backend.password_services import hash_password
from mmfdb.cli import cli
from mmfdb.repository import MFDatabase
from mmfdb.security.auth import AuthError, PermissionDenied
from mmfdb.security.login import login


def _setup(tmp_path: Path, monkeypatch) -> Path:
    db_path = tmp_path / "cif_export.db"
    MFDatabase(db_path).close()
    monkeypatch.setattr(api, "resolve_database_path", lambda *a, **k: db_path)
    monkeypatch.setattr(
        "mmfdb.store.database_resolver.resolve_database_path", lambda *a, **k: db_path
    )
    return db_path


def _user(db_path: Path, user_id: str, *, password: str = "pw", is_admin: int = 0) -> None:
    db = MFDatabase(db_path)
    db.conn.execute(
        "INSERT INTO flr_sample_users (user_id, display_name, is_admin, password_hash) "
        "VALUES (?, ?, ?, ?)",
        (user_id, user_id, is_admin, hash_password(password)),
    )
    db.conn.commit()
    db.close()


def _token(db_path: Path, user_id: str, password: str = "pw") -> dict:
    db = MFDatabase(db_path)
    try:
        result = login(db.conn, user_id=user_id, password=password)
    finally:
        db.close()
    return {"token": result["token"]}


def _analysis(db_path: Path, analysis_id: str, sample_id: str) -> None:
    db = MFDatabase(db_path)
    try:
        db.update_analysis_record(analysis_id, sample_id=sample_id, type="intensity-based")
    finally:
        db.close()


def _probe_with_spectrum(db_path: Path) -> None:
    """Seed one probe and emission spectrum so the extension categories fill."""
    db = MFDatabase(db_path)
    try:
        type_id = db.add_probe_type("organic_dye", "Organic dye")
        probe_id = db.add_probe("Alexa488", type_id, category="organic_dye")
        db.add_spectrum(
            probe_id,
            "emission",
            np.array([500.0, 520.0, 540.0]),
            np.array([0.2, 1.0, 0.3]),
        )
    finally:
        db.close()


def test_export_cif_returns_text_for_the_sample_owner(tmp_path: Path, monkeypatch) -> None:
    db_path = _setup(tmp_path, monkeypatch)
    _user(db_path, "alice")
    alice = _token(db_path, "alice")
    api.register_sample("s_cif", auth=alice)
    _analysis(db_path, "analysis_cif", "s_cif")

    result = api.export_cif(analysis_id="analysis_cif", auth=alice)

    assert result["analysis_id"] == "analysis_cif"
    assert "_flr_fret_analysis" in result["text"]
    assert "analysis_cif" in result["text"]


def test_export_cif_writes_a_file_when_asked(tmp_path: Path, monkeypatch) -> None:
    db_path = _setup(tmp_path, monkeypatch)
    _user(db_path, "alice")
    alice = _token(db_path, "alice")
    api.register_sample("s_cif", auth=alice)
    _analysis(db_path, "analysis_cif", "s_cif")
    out = tmp_path / "nested" / "export.cif"

    result = api.export_cif(analysis_id="analysis_cif", output_path=str(out), auth=alice)

    assert result["output_path"] == str(out)
    assert "text" not in result
    assert "_flr_fret_analysis" in out.read_text()


def test_export_cif_omits_the_extension_categories_on_request(
    tmp_path: Path, monkeypatch
) -> None:
    db_path = _setup(tmp_path, monkeypatch)
    _user(db_path, "alice")
    alice = _token(db_path, "alice")
    api.register_sample("s_cif", auth=alice)
    _analysis(db_path, "analysis_cif", "s_cif")
    _probe_with_spectrum(db_path)

    with_ext = api.export_cif(analysis_id="analysis_cif", auth=alice)["text"]
    without_ext = api.export_cif(
        analysis_id="analysis_cif", include_extension=False, auth=alice
    )["text"]

    assert "probe_spectrum" in with_ext
    assert "probe_spectrum" not in without_ext


def test_export_cif_resolves_the_first_analysis_when_none_is_given(
    tmp_path: Path, monkeypatch
) -> None:
    db_path = _setup(tmp_path, monkeypatch)
    _user(db_path, "alice")
    alice = _token(db_path, "alice")
    api.register_sample("s_cif", auth=alice)
    _analysis(db_path, "analysis_a", "s_cif")

    assert api.export_cif(auth=alice)["analysis_id"] == "analysis_a"


def test_export_cif_refuses_an_unreadable_sample(tmp_path: Path, monkeypatch) -> None:
    db_path = _setup(tmp_path, monkeypatch)
    _user(db_path, "alice")
    _user(db_path, "bob")
    alice, bob = _token(db_path, "alice"), _token(db_path, "bob")
    api.register_sample("s_cif", auth=alice)
    _analysis(db_path, "analysis_cif", "s_cif")

    with pytest.raises(PermissionDenied):
        api.export_cif(analysis_id="analysis_cif", auth=bob)


def test_export_cif_refuses_anonymous_callers(tmp_path: Path, monkeypatch) -> None:
    db_path = _setup(tmp_path, monkeypatch)
    _user(db_path, "alice")
    api.register_sample("s_cif", auth=_token(db_path, "alice"))
    _analysis(db_path, "analysis_cif", "s_cif")

    with pytest.raises(AuthError):
        api.export_cif(analysis_id="analysis_cif")


def test_export_cif_without_an_analysis_row_is_admin_only(tmp_path: Path, monkeypatch) -> None:
    db_path = _setup(tmp_path, monkeypatch)
    _user(db_path, "alice")
    _user(db_path, "root", is_admin=1)

    with pytest.raises(AuthError):
        api.export_cif(analysis_id="missing", auth=_token(db_path, "alice"))
    assert "data_" in api.export_cif(analysis_id="missing", auth=_token(db_path, "root"))["text"]


def test_cli_export_cif_writes_the_document_to_stdout(tmp_path: Path, monkeypatch) -> None:
    db_path = _setup(tmp_path, monkeypatch)
    _user(db_path, "alice")
    alice = _token(db_path, "alice")
    api.register_sample("s_cif", auth=alice)
    _analysis(db_path, "analysis_cif", "s_cif")

    result = CliRunner().invoke(
        cli, ["export", "cif", "--analysis", "analysis_cif", "--token", alice["token"]]
    )

    assert result.exit_code == 0, result.output
    assert "_flr_fret_analysis" in result.output


def test_cli_export_cif_writes_a_file_and_reports_it(tmp_path: Path, monkeypatch) -> None:
    db_path = _setup(tmp_path, monkeypatch)
    _user(db_path, "alice")
    alice = _token(db_path, "alice")
    api.register_sample("s_cif", auth=alice)
    _analysis(db_path, "analysis_cif", "s_cif")
    out = tmp_path / "cli_export.cif"

    result = CliRunner().invoke(
        cli,
        [
            "export", "cif",
            "--analysis", "analysis_cif",
            "--output", str(out),
            "--token", alice["token"],
        ],
    )

    assert result.exit_code == 0, result.output
    assert str(out) in result.output
    assert "_flr_fret_analysis" in out.read_text()


def test_cli_export_cif_reports_a_denied_export_as_a_clean_error(
    tmp_path: Path, monkeypatch
) -> None:
    db_path = _setup(tmp_path, monkeypatch)
    _user(db_path, "alice")
    api.register_sample("s_cif", auth=_token(db_path, "alice"))
    _analysis(db_path, "analysis_cif", "s_cif")

    result = CliRunner().invoke(cli, ["export", "cif", "--analysis", "analysis_cif"])

    assert result.exit_code != 0
    assert "Authentication" in result.output
