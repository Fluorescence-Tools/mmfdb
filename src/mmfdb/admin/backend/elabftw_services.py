"""Authenticated MMFDB Admin services for eLabFTW synchronization.

Remote credentials live only inside a bounded, owner-scoped in-memory registry.
The JSON-RPC client receives an opaque connection handle after a successful
``/info`` probe, so subsequent calls never retransmit or expose the API key.
"""

from __future__ import annotations

import json
import secrets
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from mmfdb.adapters.elabftw import (
    ELabFTWClient,
    mmfdb_experiment_id,
    mmfdb_to_remote_experiment,
    remote_to_mmfdb_experiment,
)
from mmfdb.repository import MFDatabase
from mmfdb.security.auth import (
    PERM_READ,
    PERM_WRITE,
    PermissionDenied,
    create_default_acl_for_object,
    principal_from_rpc_auth,
    require_access,
    require_authenticated,
)
from mmfdb.store.database_resolver import resolve_database_path

CONNECTION_TTL_SECONDS = 60 * 60
MAX_CONNECTIONS_PER_USER = 4
MAX_IMPORT_BATCH = 100
ELABFTW_EXPERIMENT_TYPE = "eLabFTW Experiment"


@dataclass(slots=True)
class _Connection:
    owner_user_id: str
    client: Any
    created_at: float
    last_used_at: float


class ELabFTWConnectionRegistry:
    """Bounded process-local vault for remote clients and their API keys."""

    def __init__(
        self,
        *,
        ttl_seconds: float = CONNECTION_TTL_SECONDS,
        max_connections_per_user: int = MAX_CONNECTIONS_PER_USER,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Configure connection lifetime, per-user capacity, and clock."""
        if ttl_seconds <= 0:
            raise ValueError("connection TTL must be positive")
        if max_connections_per_user <= 0:
            raise ValueError("connection limit must be positive")
        self._ttl_seconds = float(ttl_seconds)
        self._max_connections_per_user = int(max_connections_per_user)
        self._clock = clock
        self._connections: dict[str, _Connection] = {}
        self._lock = threading.RLock()

    def add(self, owner_user_id: str, client: Any) -> str:
        """Store a client and return a new opaque owner-bound handle."""
        now = self._clock()
        with self._lock:
            self._purge_expired(now)
            owned = sorted(
                (
                    (connection_id, item)
                    for connection_id, item in self._connections.items()
                    if item.owner_user_id == owner_user_id
                ),
                key=lambda pair: pair[1].last_used_at,
            )
            while len(owned) >= self._max_connections_per_user:
                connection_id, _ = owned.pop(0)
                self._connections.pop(connection_id, None)
            connection_id = secrets.token_urlsafe(24)
            self._connections[connection_id] = _Connection(
                owner_user_id=owner_user_id,
                client=client,
                created_at=now,
                last_used_at=now,
            )
            return connection_id

    def get(self, owner_user_id: str, connection_id: str) -> Any:
        """Return and refresh an owned live client or deny generically."""
        now = self._clock()
        with self._lock:
            self._purge_expired(now)
            item = self._connections.get(str(connection_id or ""))
            if item is None or item.owner_user_id != owner_user_id:
                raise PermissionDenied()
            item.last_used_at = now
            return item.client

    def remove(self, owner_user_id: str, connection_id: str) -> None:
        """Discard an owned handle and its last strong client reference."""
        with self._lock:
            item = self._connections.get(str(connection_id or ""))
            if item is None or item.owner_user_id != owner_user_id:
                raise PermissionDenied()
            del self._connections[str(connection_id)]

    def _purge_expired(self, now: float) -> None:
        expired = [
            connection_id
            for connection_id, item in self._connections.items()
            if now - item.last_used_at > self._ttl_seconds
        ]
        for connection_id in expired:
            self._connections.pop(connection_id, None)


def _positive_remote_id(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("remote_id must be a positive integer")
    try:
        remote_id = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("remote_id must be a positive integer") from exc
    if remote_id <= 0:
        raise ValueError("remote_id must be a positive integer")
    return remote_id


def _load_details(raw: Any) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(str(raw))
    except json.JSONDecodeError:
        return {"legacy_details": str(raw)}
    return value if isinstance(value, dict) else {"legacy_details": value}


def _experiment_type_id(db: MFDatabase) -> int:
    for row in db.get_experiment_types():
        if row["name"] == ELABFTW_EXPERIMENT_TYPE:
            return int(row["type_id"])
    return int(
        db.add_experiment_type(
            ELABFTW_EXPERIMENT_TYPE,
            category="electronic_lab_notebook",
            description="Experiment synchronized from an eLabFTW REST API v2 instance.",
        )
    )


class ELabFTWService:
    """Stateful service facade with injected network and database boundaries."""

    def __init__(
        self,
        *,
        registry: ELabFTWConnectionRegistry | None = None,
        db_path_resolver: Callable[[], Any] = resolve_database_path,
        client_factory: Callable[..., Any] = ELabFTWClient,
    ) -> None:
        """Configure injected connection, database, and client factories."""
        self.registry = registry or ELabFTWConnectionRegistry()
        self._db_path_resolver = db_path_resolver
        self._client_factory = client_factory

    def _admin(self, db: MFDatabase, auth: Mapping[str, Any] | None) -> Any:
        principal = principal_from_rpc_auth(db.conn, dict(auth) if auth else None)
        require_authenticated(principal)
        if not principal.is_admin:
            raise PermissionDenied()
        return principal

    def connect(
        self,
        *,
        base_url: str,
        credentials: Mapping[str, Any],
        timeout: float = 15.0,
        verify_tls: bool = True,
        allow_insecure_http: bool = False,
        auth: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Probe credentials once and retain them behind an opaque handle."""
        with MFDatabase(self._db_path_resolver()) as db:
            principal = self._admin(db, auth)
        api_key = str((credentials or {}).get("token") or "").strip()
        if not api_key:
            raise ValueError("eLabFTW API key is required")
        client = self._client_factory(
            base_url,
            api_key,
            timeout=float(timeout),
            verify_tls=bool(verify_tls),
            allow_insecure_http=bool(allow_insecure_http),
        )
        info = client.info()
        connection_id = self.registry.add(str(principal.user_id), client)
        return {
            "connection_id": connection_id,
            "endpoint": client.endpoint,
            "verify_tls": bool(verify_tls),
            "info": info,
        }

    def disconnect(
        self,
        *,
        connection_id: str,
        auth: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Discard an owner-bound remote connection handle."""
        with MFDatabase(self._db_path_resolver()) as db:
            principal = self._admin(db, auth)
        self.registry.remove(str(principal.user_id), connection_id)
        return {"ok": True}

    def list_experiments(
        self,
        *,
        connection_id: str,
        query: str = "",
        page_size: int = 50,
        max_items: int = 500,
        auth: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return bounded remote experiment summaries for the Admin GUI."""
        with MFDatabase(self._db_path_resolver()) as db:
            principal = self._admin(db, auth)
        client = self.registry.get(str(principal.user_id), connection_id)
        rows = client.list_experiments(
            query=str(query or ""),
            page_size=int(page_size),
            max_items=int(max_items),
        )
        summaries = [
            {
                "id": _positive_remote_id(row.get("id")),
                "title": str(row.get("title") or ""),
                "date": row.get("date") or row.get("created_at"),
                "status": row.get("status_title") or row.get("status"),
                "modified_at": row.get("modified_at") or row.get("timestamped"),
                "tags": list(row.get("tags") or []),
            }
            for row in rows
        ]
        return {"experiments": summaries, "count": len(summaries)}

    def import_experiments(
        self,
        *,
        connection_id: str,
        remote_ids: list[int],
        conflict: str = "skip",
        sample_id: str | None = None,
        auth: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Atomically import remote experiments with an explicit conflict policy."""
        if conflict not in {"skip", "update", "error"}:
            raise ValueError("conflict must be 'skip', 'update', or 'error'")
        if not remote_ids:
            raise ValueError("at least one remote experiment id is required")
        if len(remote_ids) > MAX_IMPORT_BATCH:
            raise ValueError(f"at most {MAX_IMPORT_BATCH} experiments may be imported at once")
        normalized_ids = list(dict.fromkeys(_positive_remote_id(item) for item in remote_ids))

        with MFDatabase(self._db_path_resolver()) as db:
            principal = self._admin(db, auth)
            if sample_id:
                require_access(db.conn, principal, "sample", sample_id, PERM_READ)
        client = self.registry.get(str(principal.user_id), connection_id)

        # Fetch the complete batch before opening a write transaction. A remote
        # failure therefore cannot leave a partial local import.
        remote_rows = [client.get_experiment(remote_id) for remote_id in normalized_ids]
        for expected_id, row in zip(normalized_ids, remote_rows):
            if _positive_remote_id(row.get("id")) != expected_id:
                raise ValueError(
                    "eLabFTW returned a different experiment id than requested"
                )
        endpoint = client.endpoint

        with MFDatabase(self._db_path_resolver()) as db:
            principal = self._admin(db, auth)
            plan: list[tuple[int, dict[str, Any] | None, str]] = []
            for remote_id in normalized_ids:
                experiment_id = mmfdb_experiment_id(endpoint, remote_id)
                row = db.get_experiment(experiment_id)
                existing = dict(row) if row is not None else None
                action = "create"
                if existing is not None:
                    if conflict == "error":
                        raise ValueError(
                            f"MMFDB experiment {experiment_id!r} already exists"
                        )
                    if conflict == "skip":
                        action = "skip"
                    else:
                        require_access(
                            db.conn,
                            principal,
                            "experiment",
                            experiment_id,
                            PERM_WRITE,
                        )
                        action = "update"
                plan.append((remote_id, existing, action))

            type_id = _experiment_type_id(db)
            imported: list[dict[str, Any]] = []
            updated: list[dict[str, Any]] = []
            skipped: list[dict[str, Any]] = []
            remote_by_id = {
                _positive_remote_id(row.get("id")): row for row in remote_rows
            }
            with db.transaction():
                for remote_id, existing, action in plan:
                    experiment_id = mmfdb_experiment_id(endpoint, remote_id)
                    summary = {
                        "remote_id": remote_id,
                        "experiment_id": experiment_id,
                    }
                    if action == "skip":
                        skipped.append(summary)
                        continue
                    mapped = remote_to_mmfdb_experiment(
                        remote_by_id[remote_id],
                        endpoint=endpoint,
                        owner_user_id=str(principal.user_id),
                        type_id=type_id,
                        existing=existing,
                    )
                    if sample_id:
                        mapped["sample_id"] = sample_id
                    db.add_experiment(**mapped)
                    if action == "create":
                        create_default_acl_for_object(
                            db.conn,
                            "experiment",
                            experiment_id,
                            owner_user_id=str(principal.user_id),
                        )
                        imported.append(summary)
                    else:
                        updated.append(summary)
            return {"imported": imported, "updated": updated, "skipped": skipped}

    def export_experiment(
        self,
        *,
        connection_id: str,
        experiment_id: str,
        mode: str = "create",
        remote_id: int | None = None,
        auth: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create or explicitly update an eLabFTW experiment from MMFDB."""
        if mode not in {"create", "update"}:
            raise ValueError("mode must be 'create' or 'update'")
        if mode == "update" and remote_id is None:
            raise ValueError("remote_id is required for explicit update mode")
        if remote_id is not None:
            remote_id = _positive_remote_id(remote_id)

        with MFDatabase(self._db_path_resolver()) as db:
            principal = self._admin(db, auth)
            row = db.get_experiment(experiment_id)
            if row is None:
                raise ValueError("MMFDB experiment not found")
            require_access(
                db.conn, principal, "experiment", experiment_id, PERM_WRITE
            )
            local = dict(row)
        client = self.registry.get(str(principal.user_id), connection_id)
        payload = mmfdb_to_remote_experiment(local)
        if mode == "create":
            remote_id = client.create_experiment(payload)
        else:
            client.update_experiment(remote_id, payload)
        assert remote_id is not None

        # Record the durable cross-reference only after the remote write
        # succeeds. Repeated exports to the same target remain idempotent.
        with MFDatabase(self._db_path_resolver()) as db:
            principal = self._admin(db, auth)
            current_row = db.get_experiment(experiment_id)
            if current_row is None:
                raise ValueError("MMFDB experiment no longer exists")
            require_access(
                db.conn, principal, "experiment", experiment_id, PERM_WRITE
            )
            current = dict(current_row)
            details = _load_details(current.get("details"))
            links = [
                link
                for link in details.get("elabftw_exports", [])
                if isinstance(link, dict)
                and not (
                    link.get("endpoint") == client.endpoint
                    and link.get("remote_id") == remote_id
                )
            ]
            links.append({"endpoint": client.endpoint, "remote_id": remote_id})
            details["elabftw_exports"] = links
            with db.transaction():
                db.add_experiment(
                    experiment_id=experiment_id,
                    type_id=current.get("type_id"),
                    sample_id=current.get("sample_id"),
                    project_id=current.get("project_id"),
                    measured_by_user_id=current.get("measured_by_user_id"),
                    measured_by_device_id=current.get("measured_by_device_id"),
                    started_at=current.get("started_at"),
                    ended_at=current.get("ended_at"),
                    status=current.get("status"),
                    details=json.dumps(details, sort_keys=True, ensure_ascii=False),
                    setup_definition_id=current.get("setup_definition_id"),
                )
        return {
            "mode": mode,
            "remote_id": remote_id,
            "endpoint": client.endpoint,
        }


def register_elabftw_services(
    dispatcher_or_context: Any,
    *,
    service: ELabFTWService | None = None,
) -> ELabFTWService:
    """Register one shared eLabFTW service instance and return it for lifecycle use."""
    dispatcher = getattr(dispatcher_or_context, "dispatcher", dispatcher_or_context)
    active = service or ELabFTWService()
    methods = {
        "mmfdb.elabftw.connect": active.connect,
        "mmfdb.elabftw.disconnect": active.disconnect,
        "mmfdb.elabftw.experiments.list": active.list_experiments,
        "mmfdb.elabftw.experiments.import": active.import_experiments,
        "mmfdb.elabftw.experiments.export": active.export_experiment,
    }
    for name, handler in methods.items():
        dispatcher.register(name, lambda params, _handler=handler: _handler(**params))
    return active


__all__ = [
    "ELabFTWConnectionRegistry",
    "ELabFTWService",
    "register_elabftw_services",
]
