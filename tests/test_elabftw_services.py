"""Authenticated MMFDB Admin service contracts for eLabFTW synchronization."""

from __future__ import annotations

import json

import pytest
from mmfdb.admin.backend.elabftw_services import (
    ELabFTWConnectionRegistry,
    ELabFTWService,
    register_elabftw_services,
)
from mmfdb.repository import MFDatabase
from mmfdb.security.auth import (
    PermissionDenied,
    create_session,
)


class FakeELabFTWClient:
    """In-memory remote API used by service contract tests."""

    endpoint = "https://lab.example/api/v2"

    def __init__(self, *, info=None, experiments=None):
        """Initialize remote instance metadata and experiments."""
        self._info = info or {"version": "5.2.0"}
        self.experiments = {int(item["id"]): item for item in experiments or []}
        self.created = []
        self.updated = []

    def info(self):
        return dict(self._info)

    def list_experiments(self, **kwargs):
        return list(self.experiments.values())

    def get_experiment(self, remote_id):
        return dict(self.experiments[int(remote_id)])

    def create_experiment(self, payload):
        self.created.append(dict(payload))
        return 91

    def update_experiment(self, remote_id, payload):
        self.updated.append((int(remote_id), dict(payload)))
        return {"id": int(remote_id), **payload}


@pytest.fixture
def authenticated_db(tmp_path):
    path = tmp_path / "elabftw.db"
    with MFDatabase(path) as db:
        for user_id, is_admin in (("alice", 1), ("bob", 1), ("member", 0)):
            db.conn.execute(
                "INSERT INTO flr_sample_users (user_id, display_name, is_admin) "
                "VALUES (?, ?, ?)",
                (user_id, user_id, is_admin),
            )
        alice = {"token": create_session(db.conn, "alice")["token"]}
        bob = {"token": create_session(db.conn, "bob")["token"]}
        member = {"token": create_session(db.conn, "member")["token"]}
        db.conn.commit()
    return path, alice, bob, member


def test_connection_registry_is_owner_scoped_bounded_and_expires() -> None:
    now = [100.0]
    registry = ELabFTWConnectionRegistry(
        ttl_seconds=10,
        max_connections_per_user=1,
        clock=lambda: now[0],
    )
    first = registry.add("alice", FakeELabFTWClient())
    second = registry.add("alice", FakeELabFTWClient())

    with pytest.raises(PermissionDenied):
        registry.get("alice", first)
    assert registry.get("alice", second).endpoint.endswith("/api/v2")
    with pytest.raises(PermissionDenied):
        registry.get("bob", second)
    now[0] = 111.0
    with pytest.raises(PermissionDenied):
        registry.get("alice", second)


def test_connect_requires_admin_and_keeps_credentials_out_of_result(authenticated_db) -> None:
    path, alice, _bob, member = authenticated_db
    created = []

    def factory(base_url, api_key, **kwargs):
        created.append((base_url, api_key, kwargs))
        return FakeELabFTWClient()

    service = ELabFTWService(
        db_path_resolver=lambda: path,
        client_factory=factory,
    )

    with pytest.raises(PermissionDenied):
        service.connect(
            base_url="https://lab.example",
            credentials={"token": "secret"},
            auth=member,
        )

    result = service.connect(
        base_url="https://lab.example",
        credentials={"token": "secret"},
        timeout=9,
        verify_tls=True,
        auth=alice,
    )
    assert result["endpoint"] == "https://lab.example/api/v2"
    assert result["info"]["version"] == "5.2.0"
    assert "secret" not in repr(result)
    assert created == [
        (
            "https://lab.example",
            "secret",
            {"timeout": 9.0, "verify_tls": True, "allow_insecure_http": False},
        )
    ]


def test_connections_cannot_be_reused_by_another_admin(authenticated_db) -> None:
    path, alice, bob, _member = authenticated_db
    service = ELabFTWService(
        db_path_resolver=lambda: path,
        client_factory=lambda *args, **kwargs: FakeELabFTWClient(),
    )
    connection_id = service.connect(
        base_url="https://lab.example",
        credentials={"token": "secret"},
        auth=alice,
    )["connection_id"]

    with pytest.raises(PermissionDenied):
        service.list_experiments(connection_id=connection_id, auth=bob)


def test_import_is_idempotent_and_update_preserves_local_fields(authenticated_db) -> None:
    path, alice, _bob, _member = authenticated_db
    remote = FakeELabFTWClient(
        experiments=[
            {
                "id": 7,
                "title": "Imported measurement",
                "body": "<p>notes</p>",
                "date": "2026-07-13",
                "status_title": "Complete",
            }
        ]
    )
    service = ELabFTWService(
        db_path_resolver=lambda: path,
        client_factory=lambda *args, **kwargs: remote,
    )
    connection_id = service.connect(
        base_url="https://lab.example",
        credentials={"token": "secret"},
        auth=alice,
    )["connection_id"]

    first = service.import_experiments(
        connection_id=connection_id,
        remote_ids=[7],
        conflict="skip",
        auth=alice,
    )
    experiment_id = first["imported"][0]["experiment_id"]
    assert first["skipped"] == []

    skipped = service.import_experiments(
        connection_id=connection_id,
        remote_ids=[7],
        conflict="skip",
        auth=alice,
    )
    assert skipped["imported"] == []
    assert skipped["skipped"] == [{"remote_id": 7, "experiment_id": experiment_id}]

    with MFDatabase(path) as db:
        row = db.get_experiment(experiment_id)
        assert row["measured_by_user_id"] == "alice"
        acl = db.conn.execute(
            "SELECT owner_user_id, mode FROM mmfdb_object_acl "
            "WHERE object_type='experiment' AND object_id=?",
            (experiment_id,),
        ).fetchone()
        assert dict(acl) == {"owner_user_id": "alice", "mode": 0o700}
        details = json.loads(row["details"])
        details["local_note"] = "preserve"
        db.add_sample("sample-linked-after-import")
        db.conn.execute(
            "UPDATE flr_experiment SET sample_id=?, details=? WHERE experiment_id=?",
            ("sample-linked-after-import", json.dumps(details), experiment_id),
        )
        db.conn.commit()

    remote.experiments[7]["title"] = "Updated title"
    updated = service.import_experiments(
        connection_id=connection_id,
        remote_ids=[7],
        conflict="update",
        auth=alice,
    )
    assert updated["updated"][0]["experiment_id"] == experiment_id
    with MFDatabase(path) as db:
        row = db.get_experiment(experiment_id)
        assert row["sample_id"] == "sample-linked-after-import"
        details = json.loads(row["details"])
        assert details["local_note"] == "preserve"
        assert details["elabftw_source"]["title"] == "Updated title"


def test_import_validates_conflict_and_batch_before_writing(authenticated_db) -> None:
    path, alice, _bob, _member = authenticated_db
    remote = FakeELabFTWClient(experiments=[{"id": 1, "title": "one"}])
    service = ELabFTWService(
        db_path_resolver=lambda: path,
        client_factory=lambda *args, **kwargs: remote,
    )
    connection_id = service.connect(
        base_url="https://lab.example",
        credentials={"token": "secret"},
        auth=alice,
    )["connection_id"]

    with pytest.raises(ValueError, match="conflict"):
        service.import_experiments(
            connection_id=connection_id,
            remote_ids=[1],
            conflict="overwrite-everything",
            auth=alice,
        )
    with pytest.raises(ValueError, match="at most 100"):
        service.import_experiments(
            connection_id=connection_id,
            remote_ids=list(range(1, 102)),
            auth=alice,
        )
    with MFDatabase(path) as db:
        assert not [
            row for row in db.get_experiments() if row["experiment_id"].startswith("elabftw:")
        ]


def test_export_create_and_explicit_update_track_remote_link(authenticated_db) -> None:
    path, alice, _bob, _member = authenticated_db
    remote = FakeELabFTWClient()
    service = ELabFTWService(
        db_path_resolver=lambda: path,
        client_factory=lambda *args, **kwargs: remote,
    )
    connection_id = service.connect(
        base_url="https://lab.example",
        credentials={"token": "secret"},
        auth=alice,
    )["connection_id"]
    with MFDatabase(path) as db:
        db.add_experiment(
            "local-1",
            measured_by_user_id="alice",
            started_at="2026-07-13T10:00:00+00:00",
            details=json.dumps({"title": "Local lifetime run", "description": "clean"}),
        )
        from mmfdb.security.auth import create_default_acl_for_object

        create_default_acl_for_object(
            db.conn, "experiment", "local-1", owner_user_id="alice"
        )
        db.conn.commit()

    created = service.export_experiment(
        connection_id=connection_id,
        experiment_id="local-1",
        mode="create",
        auth=alice,
    )
    assert created["remote_id"] == 91
    assert remote.created[0]["title"] == "Local lifetime run"
    assert remote.created[0]["date"] == "2026-07-13"

    with pytest.raises(ValueError, match="remote_id"):
        service.export_experiment(
            connection_id=connection_id,
            experiment_id="local-1",
            mode="update",
            auth=alice,
        )
    updated = service.export_experiment(
        connection_id=connection_id,
        experiment_id="local-1",
        mode="update",
        remote_id=91,
        auth=alice,
    )
    assert updated["remote_id"] == 91
    assert remote.updated[0][0] == 91

    with MFDatabase(path) as db:
        details = json.loads(db.get_experiment("local-1")["details"])
        assert details["elabftw_exports"] == [
            {
                "endpoint": "https://lab.example/api/v2",
                "remote_id": 91,
            }
        ]


def test_disconnect_zeroizes_connection_handle(authenticated_db) -> None:
    path, alice, _bob, _member = authenticated_db
    service = ELabFTWService(
        db_path_resolver=lambda: path,
        client_factory=lambda *args, **kwargs: FakeELabFTWClient(),
    )
    connection_id = service.connect(
        base_url="https://lab.example",
        credentials={"token": "secret"},
        auth=alice,
    )["connection_id"]
    assert service.disconnect(connection_id=connection_id, auth=alice) == {"ok": True}
    with pytest.raises(PermissionDenied):
        service.list_experiments(connection_id=connection_id, auth=alice)


def test_service_registration_exposes_complete_rpc_surface() -> None:
    class Dispatcher:
        def __init__(self):
            self.handlers = {}

        def register(self, name, handler):
            self.handlers[name] = handler

    dispatcher = Dispatcher()
    service = ELabFTWService(
        db_path_resolver=lambda: "unused.db",
        client_factory=lambda *args, **kwargs: FakeELabFTWClient(),
    )
    assert register_elabftw_services(dispatcher, service=service) is service
    assert set(dispatcher.handlers) == {
        "mmfdb.elabftw.connect",
        "mmfdb.elabftw.disconnect",
        "mmfdb.elabftw.experiments.list",
        "mmfdb.elabftw.experiments.import",
        "mmfdb.elabftw.experiments.export",
    }


def test_import_rejects_remote_id_substitution_before_local_write(authenticated_db) -> None:
    path, alice, _bob, _member = authenticated_db

    class SubstitutingClient(FakeELabFTWClient):
        def get_experiment(self, remote_id):
            return {"id": 999, "title": "unexpected"}

    service = ELabFTWService(
        db_path_resolver=lambda: path,
        client_factory=lambda *args, **kwargs: SubstitutingClient(),
    )
    connection_id = service.connect(
        base_url="https://lab.example",
        credentials={"token": "secret"},
        auth=alice,
    )["connection_id"]
    with pytest.raises(ValueError, match="different experiment id"):
        service.import_experiments(
            connection_id=connection_id,
            remote_ids=[7],
            auth=alice,
        )
    with MFDatabase(path) as db:
        assert not [
            row for row in db.get_experiments() if row["experiment_id"].startswith("elabftw:")
        ]
