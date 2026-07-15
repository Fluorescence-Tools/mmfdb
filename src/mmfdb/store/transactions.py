from __future__ import annotations

import logging
import uuid
from contextlib import contextmanager
from typing import Any

logger = logging.getLogger(__name__)


def _control_statement(conn: Any, sql: str) -> None:
    """Execute transaction-control SQL without retaining a server cursor."""
    cursor = conn.execute(sql)
    if getattr(conn, "dialect", "sqlite") != "sqlite":
        cursor.close()


@contextmanager
def transaction(conn: Any):
    """Context manager ensuring transaction safety and atomicity using SAVEPOINTs.

    If any error occurs within the block, the transaction is rolled back to the savepoint.
    Otherwise, the savepoint is released (committed).

    Parameters
    ----------
    conn : database connection
        SQLite or MMFDB server connection implementing the repository protocol.
    """
    if conn.in_transaction:
        sp_name = f"sp_{uuid.uuid4().hex}"
        _control_statement(conn, f"SAVEPOINT {sp_name}")
        try:
            yield conn
            _control_statement(conn, f"RELEASE SAVEPOINT {sp_name}")
        except Exception as exc:
            _control_statement(conn, f"ROLLBACK TO SAVEPOINT {sp_name}")
            _control_statement(conn, f"RELEASE SAVEPOINT {sp_name}")
            logger.error("Database transaction failed and was rolled back: %s", exc)
            raise
    else:
        _control_statement(conn, "BEGIN")
        try:
            yield conn
            conn.commit()
        except Exception as exc:
            conn.rollback()
            logger.error("Database transaction failed and was rolled back: %s", exc)
            raise
