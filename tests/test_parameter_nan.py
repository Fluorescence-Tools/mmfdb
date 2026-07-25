"""NaN parameter fields must survive a round trip through the store.

SQLite silently coerces NaN to NULL on insert into a REAL column, while ±Inf
round-trips intact. For a provenance store that is the most misleading loss
possible: a diverged fit's ``standard_error = NaN`` lands as NULL and becomes
indistinguishable from "no error was computed". Singular covariance matrices
make this a routine outcome, not a corner case.

``record_parameter`` therefore flags the NaN fields in ``metadata_json`` and
``get_parameter`` restores them.
"""

from __future__ import annotations

import math
import os
import uuid

import pytest

from mmfdb.repository import NAN_FIELDS_KEY, MFDatabase

#: Every numeric ``mmfdb_parameter`` column that NaN can be written to.
NUMERIC_FIELDS = (
    "value",
    "standard_error",
    "confidence_interval_low",
    "confidence_interval_high",
    "initial_value",
    "lower_bound",
    "upper_bound",
)


@pytest.fixture
def db(tmp_path):
    database = MFDatabase(os.path.join(tmp_path, "params.db"))
    database.record_operation(
        operation_id="op1", operation_type="analysis", status="succeeded"
    )
    yield database
    database.close()


def _record(db, **kwargs) -> str:
    parameter_uuid = str(uuid.uuid4())
    db.record_parameter(
        parameter_uuid=parameter_uuid,
        operation_id="op1",
        name=kwargs.pop("name", "p"),
        **kwargs,
    )
    return parameter_uuid


def test_sqlite_really_does_drop_nan(db):
    """The premise: the raw column cannot hold NaN, but keeps +/-Inf.

    If this ever stops being true the workaround below can be deleted.
    """
    uid = _record(db, value=float("nan"))
    raw = db.conn.execute(
        "SELECT value FROM mmfdb_parameter WHERE parameter_uuid = ?", (uid,)
    ).fetchone()[0]
    assert raw is None

    uid = _record(db, name="q", value=float("inf"))
    raw = db.conn.execute(
        "SELECT value FROM mmfdb_parameter WHERE parameter_uuid = ?", (uid,)
    ).fetchone()[0]
    assert raw == float("inf")


@pytest.mark.parametrize("field", NUMERIC_FIELDS)
def test_nan_round_trips_for_every_numeric_field(db, field):
    """Each numeric column restores NaN rather than reporting NULL."""
    uid = _record(db, **{field: float("nan")})
    restored = db.get_parameter(uid)

    assert restored is not None
    assert math.isnan(restored[field]), f"{field} came back as {restored[field]!r}"


def test_a_nan_error_is_distinguishable_from_an_absent_one(db):
    """The point of the exercise: NaN and 'not recorded' must not look alike."""
    diverged = _record(db, name="tau", value=4.2, standard_error=float("nan"))
    not_computed = _record(db, name="tau2", value=4.2)

    assert math.isnan(db.get_parameter(diverged)["standard_error"])
    assert db.get_parameter(not_computed)["standard_error"] is None


def test_several_nan_fields_are_all_restored(db):
    """A parameter may be NaN in more than one field at once."""
    uid = _record(
        db,
        name="tau",
        value=4.2,
        standard_error=float("nan"),
        initial_value=float("nan"),
        lower_bound=float("nan"),
        upper_bound=10.0,
    )
    restored = db.get_parameter(uid)

    assert restored["value"] == 4.2
    assert restored["upper_bound"] == 10.0
    for field in ("standard_error", "initial_value", "lower_bound"):
        assert math.isnan(restored[field]), field


def test_caller_metadata_is_preserved_alongside_the_marker(db):
    """Flagging NaN fields must not clobber what the caller stored."""
    uid = _record(db, value=float("nan"), metadata={"note": "kept", "n": 3})
    restored = db.get_parameter(uid)

    metadata = restored["metadata_json"]
    if isinstance(metadata, str):
        import json

        metadata = json.loads(metadata)
    assert metadata["note"] == "kept"
    assert metadata["n"] == 3
    assert metadata[NAN_FIELDS_KEY] == ["value"]


def test_clean_parameters_gain_no_metadata(db):
    """No NaN means no marker -- the common path stays exactly as it was."""
    uid = _record(db, value=1.0, standard_error=0.1)
    restored = db.get_parameter(uid)

    assert restored["value"] == 1.0
    assert restored["standard_error"] == 0.1
    assert restored["metadata_json"] in (None, "", "null")


def test_infinities_are_untouched(db):
    """+/-Inf already survive the column, so they must not be rewritten."""
    uid = _record(db, value=float("inf"), lower_bound=float("-inf"))
    restored = db.get_parameter(uid)

    assert restored["value"] == float("inf")
    assert restored["lower_bound"] == float("-inf")
    assert restored["metadata_json"] in (None, "", "null")


def test_upsert_clears_a_stale_nan_marker(db):
    """Rewriting a parameter with a real value must not keep reporting NaN."""
    parameter_uuid = str(uuid.uuid4())
    db.record_parameter(
        parameter_uuid=parameter_uuid,
        operation_id="op1",
        name="tau",
        value=float("nan"),
    )
    assert math.isnan(db.get_parameter(parameter_uuid)["value"])

    db.record_parameter(
        parameter_uuid=parameter_uuid,
        operation_id="op1",
        name="tau",
        value=3.5,
    )
    restored = db.get_parameter(parameter_uuid)
    assert restored["value"] == 3.5, "stale NaN marker resurrected on upsert"
