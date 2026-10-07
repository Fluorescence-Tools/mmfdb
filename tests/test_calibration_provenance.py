"""PRD-05: calibration provenance — registration, usage links, staleness.

Calibrations are ``calibration_data`` artifacts produced by a ``calibration`` operation
(``register_calibration``, with a ``method``/``notes`` for user-provided literature
values). A downstream result records its use with a ``calibrated_by`` edge; a use goes
stale when a newer calibration of the same type lands. The query side complements the
artifact-centric ``Lineage.impact_of`` (PRD-21).
"""

from __future__ import annotations

import os

import pytest
from mmfdb import models
from mmfdb.lifecycle.staleness import (
    find_stale_calibration_uses,
    record_calibration_use,
)
from mmfdb.provenance.result_registry import (
    register_calibration,
    register_raw_measurement,
    set_global_db,
)
from mmfdb.repository import MFDatabase


@pytest.fixture
def db(tmp_path):
    database = MFDatabase(os.path.join(tmp_path, "cal.db"))
    try:
        yield database
    finally:
        set_global_db(None)
        database.close()


def _raw(db, tmp_path, name="ref.ptu"):
    f = os.path.join(tmp_path, name)
    with open(f, "wb") as fh:
        fh.write(b"\x00\x01\x02")
    return register_raw_measurement(f, db=db)


def test_calibrated_by_is_in_the_vocabulary():
    # PRD-05 adds calibrated_by to the relationship_type enum (was forward-referenced
    # by the lineage USAGE_RELATIONSHIPS).
    assert "calibrated_by" in models.RELATIONSHIP_TYPES


def test_register_calibration_creates_artifact_linked_to_reference(db, tmp_path):
    ref = _raw(db, tmp_path)
    cal = register_calibration(
        data={"g_factor": 1.02},
        calibration_type="g_factor",
        parent_artifact_id=ref,
        parameters={"g_factor": 1.02},
        method="tail_matching",
        db=db,
    )
    assert cal
    kind = db.conn.execute(
        "SELECT artifact_kind FROM mmfdb_artifact WHERE artifact_id = ?", (cal,)
    ).fetchone()[0]
    assert kind == "calibration_data"
    # register_result records a derived_from edge from the calibration to its reference
    rel = db.conn.execute(
        "SELECT relationship_type FROM mmfdb_edge "
        "WHERE source_node_id = ? AND target_node_id = ?",
        (cal, ref),
    ).fetchone()
    assert rel is not None and rel[0] == "derived_from"


def test_user_provided_calibration_has_no_parent(db):
    # "God given" literature value: method=user_provided, no reference measurement.
    cal = register_calibration(
        data={"R0": 54.0},
        calibration_type="forster_radius",
        parameters={"R0": {"value": 54.0, "error": 2.0}},
        method="user_provided",
        notes="Hellenkamp et al. 2018",
        db=db,
    )
    assert cal
    # no derived_from edge (no parent), but the method/notes are on the artifact
    import json

    meta = json.loads(
        db.conn.execute(
            "SELECT metadata_json FROM mmfdb_artifact WHERE artifact_id = ?", (cal,)
        ).fetchone()[0]
    )
    assert meta["method"] == "user_provided"
    assert "Hellenkamp" in meta["notes"]


def test_no_stale_when_using_the_only_calibration(db, tmp_path):
    ref = _raw(db, tmp_path)
    cal = register_calibration(
        data={"g_factor": 1.02}, calibration_type="g_factor",
        parent_artifact_id=ref, db=db,
    )
    fit = _raw(db, tmp_path, "fit_result")  # stand-in consumer artifact
    record_calibration_use(
        db, used_by_id=fit, calibration_artifact_id=cal, used_by_type="artifact"
    )
    assert find_stale_calibration_uses(db) == []


def test_stale_detected_when_newer_calibration_of_same_type_lands(db, tmp_path):
    ref = _raw(db, tmp_path)
    old_cal = register_calibration(
        data={"g_factor": 1.02}, calibration_type="g_factor",
        parent_artifact_id=ref, db=db,
    )
    fit = _raw(db, tmp_path, "fit_result")
    record_calibration_use(
        db, used_by_id=fit, calibration_artifact_id=old_cal, used_by_type="artifact"
    )
    # a newer g_factor calibration supersedes the one the fit used
    new_cal = register_calibration(
        data={"g_factor": 1.05}, calibration_type="g_factor",
        parent_artifact_id=ref, db=db,
    )
    # an unrelated calibration of a different type must not trigger staleness
    register_calibration(
        data={"gamma": 0.9}, calibration_type="gamma", parent_artifact_id=ref, db=db
    )

    stale = find_stale_calibration_uses(db)
    assert len(stale) == 1
    s = stale[0]
    assert s.used_by_id == fit
    assert s.calibration_type == "g_factor"
    assert s.used_artifact_id == old_cal
    assert s.latest_artifact_id == new_cal


def test_lineage_impact_follows_calibrated_by(db, tmp_path):
    # The PRD-05 <-> PRD-21 loop: a calibrated_by edge is a USAGE_RELATIONSHIP, so a
    # calibration's impact (what_used) reaches the result that consumed it.
    ref = _raw(db, tmp_path)
    cal = register_calibration(
        data={"g_factor": 1.02}, calibration_type="g_factor",
        parent_artifact_id=ref, db=db,
    )
    fit = _raw(db, tmp_path, "fit_result")
    record_calibration_use(
        db, used_by_id=fit, calibration_artifact_id=cal, used_by_type="artifact"
    )
    assert fit in db.lineage.what_used(cal)


@pytest.mark.parametrize("actor", [None, "calibration_actor"])
def test_calibration_forwards_session_to_artifact_operation_and_acl(db, monkeypatch, actor):
    """The public wrapper preserves resolved ownership and its default behavior."""
    from mmfdb.provenance import result_registry
    from mmfdb.security.session import SessionContext

    monkeypatch.setattr(result_registry, "_resolve_active_user_id", lambda: "registry_fallback")
    session = SessionContext(user_id=actor, db=db) if actor else None
    calibration = register_calibration(
        {"g_factor": 1.25}, "g_factor", db=db, session=session,
    )
    assert calibration
    expected = actor or "registry_fallback"
    row = db.conn.execute(
        "SELECT a.created_by_user_id, o.operator_user_id, aa.owner_user_id, oa.owner_user_id "
        "FROM mmfdb_artifact a "
        "JOIN mmfdb_operation_artifact link ON link.artifact_id=a.artifact_id "
        "AND link.direction='output' "
        "JOIN mmfdb_operation o ON o.operation_id=link.operation_id "
        "JOIN mmfdb_object_acl aa ON aa.object_type='artifact' AND aa.object_id=a.artifact_id "
        "JOIN mmfdb_object_acl oa ON oa.object_type='operation' AND oa.object_id=o.operation_id "
        "WHERE a.artifact_id=?", (calibration,),
    ).fetchone()
    assert tuple(row) == (expected,) * 4
    assert db.list_artifact_owners(calibration) == [expected]
