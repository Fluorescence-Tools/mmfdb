"""Request-scoped resources shared by MMFDB API transports.

This module deliberately has no dependency on the admin/RPC host.  A transport
owns the context lifetime; API functions only borrow the database and principal.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from os import PathLike
from typing import Any

from mmfdb.repository import MFDatabase
from mmfdb.security.auth import Principal, principal_from_rpc_auth
from mmfdb.store import database_resolver


@dataclass(frozen=True, slots=True)
class APIRequestContext:
    """Resources that must be created at most once for one API request."""

    database: MFDatabase
    principal: Principal


_current_context: ContextVar[APIRequestContext | None] = ContextVar(
    "mmfdb_api_request_context", default=None
)


def current_api_context() -> APIRequestContext | None:
    """Return the context bound to the current execution flow, if any."""
    return _current_context.get()


@contextmanager
def bind_api_context(context: APIRequestContext) -> Iterator[APIRequestContext]:
    """Bind a caller-owned context without taking ownership of its database."""
    token: Token[APIRequestContext | None] = _current_context.set(context)
    try:
        yield context
    finally:
        _current_context.reset(token)


@contextmanager
def open_api_context(
    auth: dict[str, Any] | None = None,
    database_path: str | PathLike[str] | None = None,
) -> Iterator[APIRequestContext]:
    """Open and authenticate one database for a direct API or transport call."""
    path = database_path if database_path is not None else database_resolver.resolve_database_path()
    with MFDatabase(path) as database:
        yield APIRequestContext(
            database=database,
            principal=principal_from_rpc_auth(database.conn, auth),
        )
