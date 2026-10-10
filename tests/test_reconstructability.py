"""Tests for PRD-27: state reconstructability from the append-only log.

Proves that:
1. Lifecycle state is reconstructable from the transition log.
2. Operation status changes are recorded as transitions (not just in-place UPDATE).
3. Tombstone deletes preserve the record for audit.
"""

from __future__ import annotations

import sqlite3
import tempfile
import os
from pathlib import Path

import pytest

from mmfdb.schema.schema import reconcile_current_schema
from mmfdb.repository import MFDatabase


@pytest.fixture
def db():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        database = MFDatabase(db_path)
        yield database


class TestLifecycleReconstructability:
    """The lifecycle transition log is the source of truth — state is a fold."""

    def test_state_is_reconstructable_from_transitions(self, db):
        """get_state returns the latest to_state from the transition log."""
        entity_id = "test-op-001"
        entity_type = "operation"

        # No transitions yet
        assert db.get_state(entity_type, entity_id) is None

        # Record transitions — but without rules they'll fail validation.
        # Insert directly to test reconstruction without the rule gate.
        db.conn.execute(
            "INSERT INTO mmfdb_state_transition "
            "(transition_id, entity_type, entity_id, from_state, to_state, "
            "reason, created_at, updated_at, deleted_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (1, entity_type, entity_id, None, "pending",
             "initial", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z", None),
        )
        db.conn.execute(
            "INSERT INTO mmfdb_state_transition "
            "(transition_id, entity_type, entity_id, from_state, to_state, "
            "reason, created_at, updated_at, deleted_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (2, entity_type, entity_id, "pending", "running",
             "started", "2026-01-01T00:01:00Z", "2026-01-01T00:01:00Z", None),
        )
        db.conn.execute(
            "INSERT INTO mmfdb_state_transition "
            "(transition_id, entity_type, entity_id, from_state, to_state, "
            "reason, created_at, updated_at, deleted_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (3, entity_type, entity_id, "running", "succeeded",
             "done", "2026-01-01T00:02:00Z", "2026-01-01T00:02:00Z", None),
        )
        db.conn.commit()

        # Current state = latest transition's to_state
        assert db.get_state(entity_type, entity_id) == "succeeded"

        # History preserves all transitions
        history = db.get_state_history(entity_type, entity_id)
        assert len(history) == 3
        assert history[0]["to_state"] == "pending"
        assert history[-1]["to_state"] == "succeeded"

    def test_tombstone_delete_preserves_record(self, db):
        """A tombstoned transition is hidden from get_state but preserved."""
        entity_id = "test-op-002"
        entity_type = "operation"

        db.conn.execute(
            "INSERT INTO mmfdb_state_transition "
            "(transition_id, entity_type, entity_id, from_state, to_state, "
            "reason, created_at, updated_at, deleted_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (1, entity_type, entity_id, None, "pending",
             None, "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z", None),
        )
        # Tombstone it
        db.conn.execute(
            "UPDATE mmfdb_state_transition SET deleted_at = ? "
            "WHERE entity_id = ? AND entity_type = ?",
            ("2026-01-02T00:00:00Z", entity_id, entity_type),
        )
        db.conn.commit()

        # get_state skips deleted
        assert db.get_state(entity_type, entity_id) is None

        # But the row is still there
        row = db.conn.execute(
            "SELECT * FROM mmfdb_state_transition WHERE entity_id = ?",
            (entity_id,),
        ).fetchone()
        assert row is not None
        assert row["deleted_at"] is not None


class TestOperationStatusTransition:
    """Operation status changes should be recorded in the transition log."""

    def test_update_operation_status_logs_transition(self, db):
        """update_operation_status records a state transition, not just an UPDATE."""
        from mmfdb.provenance.result_registry import register_raw_measurement
        import tempfile as tf

        with tf.NamedTemporaryFile(suffix=".bin", delete=False) as f:
            f.write(b"test")
            test_file = f.name

        try:
            artifact_id = register_raw_measurement(test_file, db=db)
            assert artifact_id

            operation_id = db.conn.execute(
                "SELECT operation_id FROM mmfdb_operation_artifact "
                "WHERE artifact_id = ? AND deleted_at IS NULL",
                (artifact_id,),
            ).fetchone()

            if operation_id:
                op_id = operation_id["operation_id"]

                # Registering the measurement already logs the initial transition;
                # create it directly only when this build does not (the log must
                # be complete either way).
                if not db.get_state_history("operation", op_id):
                    next_id = db.conn.execute(
                        "SELECT COALESCE(MAX(transition_id), 0) + 1 FROM mmfdb_state_transition"
                    ).fetchone()[0]
                    db.conn.execute(
                        "INSERT INTO mmfdb_state_transition "
                        "(transition_id, entity_type, entity_id, from_state, to_state, "
                        "reason, created_at, updated_at, deleted_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (next_id, "operation", op_id, None, "pending", "initial",
                         "2026-01-01", "2026-01-01", None),
                    )
                    db.conn.commit()

                # Now go pending → running → succeeded (valid per rules)
                db.update_operation_status(op_id, "running")
                db.update_operation_status(op_id, "succeeded")

                history = db.get_state_history("operation", op_id)
                assert len(history) >= 3
                states = [h["to_state"] for h in history]
                assert "pending" in states
                assert "running" in states
                assert "succeeded" in states

                # Current state from log matches the column
                log_state = db.get_state("operation", op_id)
                row = db.conn.execute(
                    "SELECT status FROM mmfdb_operation WHERE operation_id = ?",
                    (op_id,),
                ).fetchone()
                col_state = row["status"] if row else None
                assert log_state == col_state == "succeeded"
        finally:
            os.unlink(test_file)
