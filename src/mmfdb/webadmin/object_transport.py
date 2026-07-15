"""Streaming HTTP bridge to MMFDB's authenticated object-store contract."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mmfdb.admin.backend import services
from mmfdb.repository import MFDatabase
from mmfdb.security.auth import PERM_READ, require_access
from mmfdb.store.database_resolver import resolve_database_path


def store_uploaded_file(
    path: Path,
    *,
    filename: str,
    mime_type: str | None,
    metadata: dict[str, Any] | None,
    auth: dict[str, Any],
) -> dict[str, Any]:
    """Register a bounded temporary upload using the canonical auth/ACL rules.

    This is the path-based counterpart to ``put_object_handler``. The HTTP
    boundary owns and deletes *path*; the repository streams it into whichever
    configured object-store backend is active.
    """
    result = services.store_object_payload(
        trusted_path=path,
        filename=filename,
        mime_type=mime_type,
        metadata=metadata,
        auth=auth,
    )
    return dict(result["object"])


def authorized_download(
    object_uuid: str, *, auth: dict[str, Any]
) -> tuple[Path, dict[str, Any]]:
    """Authorize an object read and return its verified local/cache path."""
    info_result = services.get_object_info_handler(object_uuid=object_uuid, auth=auth)
    if not info_result.get("ok"):
        raise KeyError(object_uuid)
    info = dict(info_result["object"])
    with MFDatabase(resolve_database_path()) as db:
        # Repeat the ACL check on the connection used to resolve the path. This
        # closes the gap between metadata authorization and materialization.
        principal = services._require_auth(auth, db.conn)
        require_access(db.conn, principal, "object", object_uuid, PERM_READ)
        path = db.get_object_path(object_uuid)
    if not path.is_file():
        raise FileNotFoundError(object_uuid)
    return path, info
