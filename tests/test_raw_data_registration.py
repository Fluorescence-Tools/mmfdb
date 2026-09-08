"""Registering a file that does not belong to an experiment yet.

That is the normal case: a tool imports what the user just dropped, long before
anyone has said which experiment it belongs to. ``raw_data.experiment_id`` is a
foreign key, so the value for "none yet" has to be NULL — an empty string is a
*value*, it matches no row, and SQLite rejects the insert with
``FOREIGN KEY constraint failed``. The handler used to coerce a missing id to
``""`` and every such registration failed.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def services(tmp_path):
    """The measurement services bound to a fresh database."""
    from mmfdb.admin.backend import measurement_services as svc

    db_path = tmp_path / "mmfdb.sqlite"
    svc.configure(str(db_path)) if hasattr(svc, "configure") else None
    svc._resolved_db_path = str(db_path)
    return svc


def test_a_file_registers_without_an_experiment(services, tmp_path):
    """No experiment id means NULL, not ''."""
    measurement = tmp_path / "m000.ptu"
    measurement.write_bytes(b"not really a ptu")

    result = services.register_raw_data_handler(
        raw_data={
            "file_path": str(measurement),
            "data_type": "TTTR",
            "storage_mode": "local_file",
        }
    )
    assert result.get("ok") is True, result
    assert not result.get("experiment_id")


def test_an_empty_experiment_id_is_treated_as_none(services, tmp_path):
    """A caller that sends '' means "none", and must not be rejected for it."""
    measurement = tmp_path / "m001.ptu"
    measurement.write_bytes(b"not really a ptu")

    result = services.register_raw_data_handler(
        raw_data={
            "file_path": str(measurement),
            "data_type": "TTTR",
            "storage_mode": "local_file",
            "experiment_id": "",
        }
    )
    assert result.get("ok") is True, result
