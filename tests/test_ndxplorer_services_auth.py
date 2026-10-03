"""The ndxplorer services sit behind the same auth boundary as the others.

A logged-in client sends ``auth`` with every call; the handlers used to get it
as a keyword and fail, and an anonymous call could record a processing run.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mmfdb.admin.backend import ndxplorer_services
from mmfdb.repository import MFDatabase
from mmfdb.security.auth import create_session


class _Dispatcher:
    def __init__(self):
        self.handlers = {}

    def register(self, name, handler):
        self.handlers[name] = handler


@pytest.fixture
def setup(tmp_path: Path):
    path = tmp_path / "ndx.db"
    with MFDatabase(path) as db:
        db.add_user("u", display_name="U", is_admin=0, allow_passwordless_login=1)
        token = create_session(db.conn, "u")["token"]
        db.add_sample("s")
        db.add_experiment("e", sample_id="s", status="complete")
        run = db.add_processing_run(experiment_id="e", input_raw_data_ids=[], settings={},
                                    status="succeeded")
        product = db.add_processed_data_product(
            processing_id=run, product_type="derived_product", storage_mode="folder",
            folder_path=str(tmp_path), checksum="1" * 64, validation_status="valid")
    dispatcher = _Dispatcher()
    ndxplorer_services.register_ndxplorer_services(dispatcher, db_path=str(path))
    yield dispatcher.handlers["ndxplorer.record_analysis"], token, product
    ndxplorer_services.register_ndxplorer_services(_Dispatcher())  # unpin the path


def _params(product, **extra):
    return dict(experiment_id="e", input_processed_data_ids=[product],
                analysis_type="selection", settings={"gate": []},
                products=[{"product_type": "selection_mask", "storage_mode": "embedded_json",
                           "data": {"mask": [True, False]}, "validation_status": "valid"}],
                **extra)


def test_an_authenticated_call_records_the_run(setup):
    record, token, product = setup
    reply = record(_params(product, auth={"token": token}))
    assert reply["ok"], reply
    assert reply["processing_run"]["processing_type"] == "ndxplorer_selection"


def test_an_anonymous_call_is_refused(setup):
    record, _token, product = setup
    with pytest.raises(Exception):
        record(_params(product))
