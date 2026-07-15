"""Append-only MMFDB persistence for the operation-history event log (PRD-43).

Each recorded interactive action becomes one row in ``mmfdb_event_log``; the
in-memory ``OperationHistory`` is a projection of these rows.  Every function is
best-effort and degrades to a no-op / empty result when no MMFDB connection is
available, mirroring :func:`mmfdb.provenance.result_registry.register_operation`.
The store is append-only: rows are inserted, never updated in place, and re-inserts
of the same ``event_id`` are ignored (idempotent restore/replay).
"""

from __future__ import annotations

import json
import logging
import typing
import uuid

logger = logging.getLogger(__name__)

# Columns written, in order. Audit columns (created_at/updated_at/deleted_at) are
# defaulted by the schema and not set here.
_COLUMNS: tuple[str, ...] = (
    "event_id",
    "seq",
    "project_id",
    "action_type",
    "operation_type",
    "summary",
    "payload_json",
    "source_uid",
    "target_uid",
    "operation_id",
    "event_timestamp",
    "replayable",
    "side_effect_class",
    "history_version",
)


def _resolve_conn(db: typing.Any) -> typing.Any:
    """Return a live sqlite connection, or ``None`` when MMFDB is unavailable."""
    if db is None:
        from mmfdb.provenance.result_registry import _get_global_db

        db = _get_global_db()
    if db is None:
        return None
    return getattr(db, "conn", None)


def _ensure_index(conn: typing.Any) -> None:
    """Guarantee a UNIQUE index on event_id so re-inserts are idempotent."""
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_mmfdb_event_log_event_id ON mmfdb_event_log(event_id)"
    )


def _begin_write(conn: typing.Any) -> tuple[str | None, bool]:
    """Start a serialized write without taking ownership of an outer transaction."""
    if conn.in_transaction:
        savepoint = f"event_log_{uuid.uuid4().hex}"
        conn.execute(f"SAVEPOINT {savepoint}")
        return savepoint, False
    conn.execute("BEGIN IMMEDIATE")
    return None, True


def _finish_write(conn: typing.Any, savepoint: str | None, owns_transaction: bool) -> None:
    if owns_transaction:
        conn.commit()
    else:
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")


def _rollback_write(conn: typing.Any, savepoint: str | None, owns_transaction: bool) -> None:
    if owns_transaction:
        conn.rollback()
    else:
        conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")


def append_event(
    event: dict[str, typing.Any],
    *,
    project_id: str | None = None,
    operation_type: str | None = None,
    operation_id: str | None = None,
    history_version: str | None = None,
    db: typing.Any = None,
    strict: bool = False,
) -> bool:
    """Append one history event to ``mmfdb_event_log`` (best-effort, append-only).

    Returns ``True`` if a row was written, ``False`` when no MMFDB is available or
    the write failed (history stays authoritative in memory either way). Re-inserts
    of an existing ``event_id`` are ignored.
    """
    conn = _resolve_conn(db)
    if conn is None:
        return False
    event_id = event.get("event_id")
    if not event_id:
        return False
    savepoint: str | None = None
    owns_transaction = False
    try:
        savepoint, owns_transaction = _begin_write(conn)
        _ensure_index(conn)
        if conn.execute(
            "SELECT 1 FROM mmfdb_event_log WHERE event_id = ?",
            (str(event_id),),
        ).fetchone():
            _finish_write(conn, savepoint, owns_transaction)
            return True
        seq = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM mmfdb_event_log").fetchone()[0]
        payload = event.get("payload") or {}
        row = {
            "event_id": str(event_id),
            "seq": int(seq),
            "project_id": project_id,
            "action_type": str(event.get("action_type", "")),
            "operation_type": operation_type,
            "summary": event.get("summary"),
            "payload_json": json.dumps(payload, sort_keys=True) if payload else None,
            "source_uid": event.get("source_uid"),
            "target_uid": event.get("target_uid"),
            "operation_id": operation_id,
            "event_timestamp": event.get("timestamp"),
            "replayable": 1 if payload.get("replayable", True) else 0,
            "side_effect_class": payload.get("side_effect_class"),
            "history_version": history_version,
        }
        placeholders = ", ".join("?" * len(_COLUMNS))
        conn.execute(
            f"INSERT INTO mmfdb_event_log ({', '.join(_COLUMNS)}) "
            f"VALUES ({placeholders}) ON CONFLICT(event_id) DO NOTHING",
            tuple(row[c] for c in _COLUMNS),
        )
        _finish_write(conn, savepoint, owns_transaction)
        return True
    except Exception:  # never break the live session on a persistence hiccup
        if savepoint is not None or owns_transaction:
            try:
                _rollback_write(conn, savepoint, owns_transaction)
            except Exception:
                logger.debug("append_event rollback failed", exc_info=True)
        logger.debug("append_event failed", exc_info=True)
        if strict:
            raise
        return False


def scope_events(
    event_ids: typing.Iterable[str],
    *,
    project_id: str,
    operation_id: str | None = None,
    db: typing.Any = None,
    strict: bool = False,
) -> int:
    """Attach existing immutable events to a project in one explicit write.

    Event payloads remain append-only.  Scoping is separate because project
    identity is often not known when an interactive event is first recorded.
    """
    conn = _resolve_conn(db)
    ids = tuple(dict.fromkeys(str(event_id) for event_id in event_ids if event_id))
    if conn is None or not ids or not project_id:
        return 0
    savepoint: str | None = None
    owns_transaction = False
    try:
        savepoint, owns_transaction = _begin_write(conn)
        placeholders = ", ".join("?" for _ in ids)
        cursor = conn.execute(
            "UPDATE mmfdb_event_log SET "
            "project_id = COALESCE(project_id, ?), "
            "operation_id = COALESCE(operation_id, ?) "
            f"WHERE event_id IN ({placeholders})",
            (project_id, operation_id, *ids),
        )
        _finish_write(conn, savepoint, owns_transaction)
        return int(cursor.rowcount)
    except Exception:
        if savepoint is not None or owns_transaction:
            try:
                _rollback_write(conn, savepoint, owns_transaction)
            except Exception:
                logger.debug("scope_events rollback failed", exc_info=True)
        logger.debug("scope_events failed", exc_info=True)
        if strict:
            raise
        return 0


def read_events(
    *,
    project_id: str | None = None,
    limit: int | None = None,
    db: typing.Any = None,
) -> list[dict[str, typing.Any]]:
    """Read events back in log order, shaped like in-memory history events.

    Returns ``[]`` when no MMFDB is available. The returned dicts carry the same
    keys ``OperationHistory.load_events`` expects, so the in-memory history is
    rehydrated as a faithful projection of the durable log.
    """
    conn = _resolve_conn(db)
    if conn is None:
        return []
    try:
        sql = (
            "SELECT event_id, action_type, summary, payload_json, "
            "source_uid, target_uid, event_timestamp "
            "FROM mmfdb_event_log WHERE deleted_at IS NULL"
        )
        params: list[typing.Any] = []
        if project_id is not None:
            sql += " AND project_id = ?"
            params.append(project_id)
        sql += " ORDER BY seq ASC"
        if limit:
            sql += " LIMIT ?"
            params.append(int(limit))
        rows = conn.execute(sql, params).fetchall()
    except Exception:
        logger.debug("read_events failed", exc_info=True)
        return []

    events: list[dict[str, typing.Any]] = []
    for event_id, action_type, summary, payload_json, source_uid, target_uid, ts in rows:
        events.append(
            {
                "event_id": event_id,
                "action_type": action_type,
                "summary": summary or "",
                "payload": json.loads(payload_json) if payload_json else {},
                "source_uid": source_uid,
                "target_uid": target_uid,
                "timestamp": ts,
            }
        )
    return events
