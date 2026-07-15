"""Production security invariants for MMFDB trust boundaries."""

from __future__ import annotations

import base64
import sqlite3
from pathlib import Path

import pytest

import mmfdb.api as api
from mmfdb.admin.backend import services
from mmfdb.admin.backend.password_services import hash_password
from mmfdb.repository import MFDatabase
from mmfdb.security.auth import (
    AuthError,
    PermissionDenied,
    authenticate_token,
    create_session,
    create_default_acl_for_object,
)
from mmfdb.security.login import login
from mmfdb.store import database_resolver
from mmfdb.schema.schema import SCHEMA_VERSION, migrate_schema


def _add_user(db_path: Path, user_id: str, *, admin: bool = False) -> dict[str, str]:
    with MFDatabase(db_path) as db:
        db.conn.execute(
            "INSERT INTO flr_sample_users "
            "(user_id, user_uuid, display_name, is_admin, password_hash) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, f"uuid-{user_id}", user_id, int(admin), hash_password("Test-pass1!")),
        )
        session = create_session(db.conn, user_id)
        db.conn.commit()
    return {"token": session["token"]}


def _patch_service_database(monkeypatch: pytest.MonkeyPatch, db_path: Path) -> None:
    monkeypatch.setattr(services, "resolve_database_path", lambda: db_path)
    monkeypatch.setattr(services, "user_database_path", lambda: db_path)


def test_fresh_database_has_only_locked_service_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MMFDB_BOOTSTRAP_ADMIN_USER", raising=False)
    monkeypatch.delenv("MMFDB_BOOTSTRAP_ADMIN_PASSWORD", raising=False)

    with MFDatabase(tmp_path / "locked.db") as db:
        users = db.conn.execute(
            "SELECT user_id, is_admin, password_hash, allow_passwordless_login "
            "FROM flr_sample_users"
        ).fetchall()

    assert [tuple(row) for row in users] == [("user_default", 0, None, 0)]


def test_environment_bootstrap_creates_only_requested_admin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MMFDB_BOOTSTRAP_ADMIN_USER", "site-admin")
    monkeypatch.setenv("MMFDB_BOOTSTRAP_ADMIN_PASSWORD", "Site-pass1!")
    db_path = tmp_path / "bootstrapped.db"

    with MFDatabase(db_path) as db:
        users = db.conn.execute(
            "SELECT user_id, is_admin, allow_passwordless_login FROM flr_sample_users"
        ).fetchall()
        authenticated = login(db.conn, user_id="site-admin", password="Site-pass1!")

    assert {tuple(row) for row in users} == {
        ("user_default", 0, 0),
        ("site-admin", 1, 0),
    }
    assert authenticated["authenticated"] is True


def test_local_account_without_password_is_not_a_login_credential(tmp_path: Path) -> None:
    db_path = tmp_path / "no-password.db"
    with MFDatabase(db_path) as db:
        db.conn.execute(
            "INSERT INTO flr_sample_users (user_id, display_name) VALUES ('placeholder', 'Placeholder')"
        )
        db.conn.commit()
        with pytest.raises(AuthError, match="Invalid credentials"):
            login(db.conn, user_id="placeholder", password="")


def test_direct_canonical_write_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "api.db"
    MFDatabase(db_path).close()
    monkeypatch.setattr(api, "resolve_database_path", lambda: db_path)

    with pytest.raises(AuthError, match="Authentication required"):
        api.register_sample("anonymous-write")


def test_logout_revocation_is_committed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from contextlib import contextmanager
    from mmfdb.admin.backend import auth_services

    db_path = tmp_path / "logout.db"
    auth = _add_user(db_path, "alice")

    @contextmanager
    def open_db():
        with MFDatabase(db_path) as db:
            yield db

    monkeypatch.setattr(auth_services, "_get_db", open_db)
    auth_services.logout_handler(auth=auth)

    with MFDatabase(db_path) as verifier:
        assert authenticate_token(verifier.conn, auth["token"]).is_authenticated is False


def test_maintenance_and_user_directory_require_admin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "admin.db"
    MFDatabase(db_path).close()
    user_auth = _add_user(db_path, "user")
    admin_auth = _add_user(db_path, "admin-user", admin=True)
    _patch_service_database(monkeypatch, db_path)

    with pytest.raises(PermissionDenied):
        services.list_users_handler(auth=user_auth)
    with pytest.raises(PermissionDenied):
        services.backup_handler(auth=user_auth)

    assert services.list_users_handler(auth=admin_auth)["users"]
    assert Path(services.backup_handler(auth=admin_auth)["backup_path"]).exists()


def test_object_rpc_rejects_server_paths_and_enforces_acl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "objects.db"
    object_root = tmp_path / "objects"
    MFDatabase(db_path).close()
    alice = _add_user(db_path, "alice")
    bob = _add_user(db_path, "bob")
    _patch_service_database(monkeypatch, db_path)
    monkeypatch.setattr(database_resolver, "object_store_root", lambda: object_root)

    with pytest.raises(ValueError, match="server-side paths"):
        services.put_object_handler(path="/etc/passwd", auth=alice)

    result = services.put_object_bytes_handler(
        data=base64.b64encode(b"private").decode("ascii"),
        filename="private.bin",
        auth=alice,
    )
    object_uuid = result["object"]["object_uuid"]

    with pytest.raises(PermissionDenied):
        services.get_object_handler(object_uuid, auth=bob)
    with pytest.raises(PermissionDenied):
        services.delete_object_handler(object_uuid, auth=bob)

    assert base64.b64decode(services.get_object_handler(object_uuid, auth=alice)["data"]) == b"private"
    assert services.delete_object_handler(object_uuid, auth=alice)["ok"] is True


def test_identical_object_uploads_create_independent_user_references(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "dedup.db"
    object_root = tmp_path / "objects"
    MFDatabase(db_path).close()
    alice = _add_user(db_path, "alice")
    bob = _add_user(db_path, "bob")
    _patch_service_database(monkeypatch, db_path)
    monkeypatch.setattr(database_resolver, "object_store_root", lambda: object_root)
    payload = base64.b64encode(b"same content").decode("ascii")

    alice_result = services.put_object_bytes_handler(
        data=payload, filename="alice.bin", auth=alice
    )["object"]
    bob_result = services.put_object_bytes_handler(
        data=payload, filename="bob.bin", auth=bob
    )["object"]

    assert bob_result["object_uuid"] == alice_result["object_uuid"]
    assert bob_result["original_filename"] == "bob.bin"
    assert base64.b64decode(
        services.get_object_handler(alice_result["object_uuid"], auth=bob)["data"]
    ) == b"same content"
    assert services.delete_object_handler(alice_result["object_uuid"], auth=bob)[
        "refcount"
    ] == 1
    with pytest.raises(PermissionDenied):
        services.delete_object_handler(alice_result["object_uuid"], auth=bob)
    assert base64.b64decode(
        services.get_object_handler(alice_result["object_uuid"], auth=alice)["data"]
    ) == b"same content"


def test_object_rpc_rejects_oversized_payload_before_decode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "size.db"
    MFDatabase(db_path).close()
    auth = _add_user(db_path, "alice")
    _patch_service_database(monkeypatch, db_path)
    monkeypatch.setattr(services, "MAX_OBJECT_UPLOAD_BYTES", 3)

    with pytest.raises(ValueError, match="size limit"):
        services.put_object_bytes_handler(
            data=base64.b64encode(b"four").decode("ascii"),
            filename="too-large.bin",
            auth=auth,
        )


def test_dataset_open_requires_artifact_read_acl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "dataset.db"
    source = tmp_path / "private.dat"
    source.write_bytes(b"private")
    MFDatabase(db_path).close()
    alice = _add_user(db_path, "alice")
    bob = _add_user(db_path, "bob")
    _patch_service_database(monkeypatch, db_path)
    with MFDatabase(db_path) as db:
        db.register_artifact(
            "private-artifact",
            artifact_kind="raw_data",
            storage_mode="local_file",
            file_path=str(source),
            created_by_user_id="alice",
        )
        create_default_acl_for_object(
            db.conn, "artifact", "private-artifact", "alice"
        )
        db.conn.commit()

    with pytest.raises(PermissionDenied):
        services.datasets_open_handler("private-artifact", auth=bob)
    with pytest.raises(AuthError):
        services.datasets_open_handler("private-artifact")
    assert services.datasets_open_handler("private-artifact", auth=alice)[
        "local_path"
    ] == str(source)


def test_setup_and_parameter_collections_are_acl_scoped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "collections.db"
    MFDatabase(db_path).close()
    alice = _add_user(db_path, "alice")
    bob = _add_user(db_path, "bob")
    _patch_service_database(monkeypatch, db_path)
    monkeypatch.setattr(api, "resolve_database_path", lambda: db_path)

    api.save_setup("alice-setup", "Private setup", auth=alice)
    with MFDatabase(db_path) as db:
        db.record_operation(
            "alice-operation",
            "analysis",
            operator_user_id="alice",
            acl_owner_user_id="alice",
        )
        db.record_parameter(
            "alice-parameter", "alice-operation", "secret", value=42.0
        )

    assert api.list_setups(auth=bob)["setups"] == []
    assert api.list_parameters(auth=bob)["parameters"] == []
    with pytest.raises(PermissionDenied):
        api.save_setup("alice-setup", "Overwrite", auth=bob)
    assert api.get_setup("alice-setup", auth=alice)["setup"]["name"] == "Private setup"
    assert [p["parameter_uuid"] for p in api.list_parameters(auth=alice)["parameters"]] == [
        "alice-parameter"
    ]


@pytest.mark.parametrize(
    ("object_type", "save", "delete", "payload", "table", "id_column", "value_column"),
    [
        (
            "sample",
            services.save_sample_handler,
            services.delete_sample_handler,
            lambda object_id, value: {"sample_id": object_id, "description": value},
            "flr_sample",
            "sample_id",
            "description",
        ),
        (
            "experiment",
            services.save_experiment_handler,
            services.delete_experiment_handler,
            lambda object_id, value: {"experiment_id": object_id, "details": value},
            "flr_experiment",
            "experiment_id",
            "details",
        ),
        (
            "device",
            services.save_device_handler,
            services.delete_device_handler,
            lambda object_id, value: {"device_id": object_id, "name": value},
            "flr_sample_devices",
            "device_id",
            "name",
        ),
        (
            "entity",
            services.save_entity_handler,
            services.delete_entity_handler,
            lambda object_id, value: {"entity_id": object_id, "name": value},
            "entities",
            "entity_id",
            "common_name",
        ),
    ],
)
def test_legacy_rpc_mutations_are_owner_scoped_with_admin_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    object_type,
    save,
    delete,
    payload,
    table: str,
    id_column: str,
    value_column: str,
) -> None:
    """Standalone RPC users must not mutate another user's legacy objects."""
    db_path = tmp_path / f"{object_type}-authorization.db"
    MFDatabase(db_path).close()
    alice = _add_user(db_path, "alice")
    bob = _add_user(db_path, "bob")
    admin = _add_user(db_path, "site-admin", admin=True)
    _patch_service_database(monkeypatch, db_path)

    alice_id = f"alice-{object_type}"
    save(payload(alice_id, "alice-original"), auth=alice)

    with pytest.raises(PermissionDenied):
        save(payload(alice_id, "bob-overwrite"), auth=bob)
    with pytest.raises(PermissionDenied):
        delete(alice_id, auth=bob)

    with MFDatabase(db_path) as db:
        value = db.conn.execute(
            f"SELECT {value_column} FROM {table} WHERE {id_column} = ?",
            (alice_id,),
        ).fetchone()[0]
        acl = db.conn.execute(
            "SELECT owner_user_id FROM mmfdb_object_acl "
            "WHERE object_type = ? AND object_id = ? AND deleted_at IS NULL",
            (object_type, alice_id),
        ).fetchone()
    assert value == "alice-original"
    assert acl is not None and acl[0] == "alice"

    save(payload(alice_id, "alice-update"), auth=alice)
    delete(alice_id, auth=alice)
    with pytest.raises(PermissionDenied):
        save(payload(alice_id, "bob-resurrection"), auth=bob)
    save(payload(alice_id, "alice-resurrection"), auth=alice)
    delete(alice_id, auth=alice)

    admin_id = f"admin-target-{object_type}"
    save(payload(admin_id, "alice-created"), auth=alice)
    save(payload(admin_id, "admin-update"), auth=admin)
    delete(admin_id, auth=admin)


def test_structured_sample_cannot_overwrite_foreign_entity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "structured-entity-authorization.db"
    MFDatabase(db_path).close()
    alice = _add_user(db_path, "alice")
    bob = _add_user(db_path, "bob")
    _patch_service_database(monkeypatch, db_path)

    services.save_entity_handler(
        {"entity_id": "shared_entity", "name": "Shared Entity", "details": "alice"},
        auth=alice,
    )
    with pytest.raises(PermissionDenied):
        services.save_sample_handler(
            {
                "sample_id": "bob-structured",
                "entities": [{"name": "Shared Entity", "details": "bob-overwrite"}],
            },
            auth=bob,
        )

    services.save_sample_handler(
        {
            "sample_id": "alice-structured",
            "entities": [{"name": "Unique Entity", "details": "alice"}],
        },
        auth=alice,
    )
    services.save_entity_handler(
        {"entity_id": "unique_entity", "name": "Owner Update"}, auth=alice
    )


def test_legacy_child_mutations_require_parent_write_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "legacy-child-authorization.db"
    MFDatabase(db_path).close()
    alice = _add_user(db_path, "alice")
    bob = _add_user(db_path, "bob")
    _patch_service_database(monkeypatch, db_path)

    services.save_sample_handler(
        {"sample_id": "alice-sample", "description": "private"}, auth=alice
    )
    services.save_experiment_handler(
        {"experiment_id": "alice-experiment", "details": "private"}, auth=alice
    )

    with pytest.raises(PermissionDenied):
        services.save_sample_key_values_handler(
            "alice-sample", [{"key": "secret", "value": "overwrite"}], auth=bob
        )
    with pytest.raises(PermissionDenied):
        services.save_experiment_key_values_handler(
            "alice-experiment",
            [{"key": "secret", "value": "overwrite"}],
            auth=bob,
        )
    with pytest.raises(PermissionDenied):
        services.save_experiment_data_handler(
            {
                "experiment_id": "alice-experiment",
                "data_type": "result",
                "data_json": "{}",
            },
            auth=bob,
        )

    services.save_experiment_data_handler(
        {
            "experiment_id": "alice-experiment",
            "data_type": "result",
            "data_json": "{}",
        },
        auth=alice,
    )
    services.save_experiment_handler(
        {"experiment_id": "bob-experiment", "details": "private"}, auth=bob
    )
    services.save_experiment_data_handler(
        {
            "experiment_id": "bob-experiment",
            "data_type": "result",
            "data_json": "{}",
        },
        auth=bob,
    )
    with MFDatabase(db_path) as db:
        alice_data_id = db.conn.execute(
            "SELECT data_id FROM flr_experiment_data WHERE experiment_id = ?",
            ("alice-experiment",),
        ).fetchone()[0]
        bob_data_id = db.conn.execute(
            "SELECT data_id FROM flr_experiment_data WHERE experiment_id = ?",
            ("bob-experiment",),
        ).fetchone()[0]
    with pytest.raises(PermissionDenied):
        services.save_experiment_data_handler(
            {
                "data_id": bob_data_id,
                "experiment_id": "alice-experiment",
                "data_type": "result",
                "data_json": "{\"stolen\": true}",
            },
            auth=alice,
        )
    with pytest.raises(PermissionDenied):
        services.delete_experiment_data_handler(alice_data_id, auth=bob)
    assert services.delete_experiment_data_handler(alice_data_id, auth=alice)["ok"] is True


def test_direct_migration_rejects_future_schema() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE _schema_version (version INTEGER NOT NULL)")
    conn.execute("INSERT INTO _schema_version VALUES (?)", (SCHEMA_VERSION + 1,))

    with pytest.raises(RuntimeError, match="newer"):
        migrate_schema(conn)


def test_direct_migration_rejects_populated_unversioned_database() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE scientific_data (value TEXT)")

    with pytest.raises(RuntimeError, match="unversioned"):
        migrate_schema(conn)


def test_v43_migration_disables_prerelease_builtin_credentials(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy-admin.db"
    with MFDatabase(db_path) as db:
        db.conn.execute(
            "UPDATE flr_sample_users SET is_admin = 1, password_hash = ? "
            "WHERE user_id = 'user_default'",
            (hash_password("admin"),),
        )
        db.conn.execute("UPDATE _schema_version SET version = 42")
        db.conn.execute("UPDATE mmfdb_schema_version SET version = 42")
        db.conn.commit()

    with MFDatabase(db_path) as migrated:
        row = migrated.conn.execute(
            "SELECT is_admin, password_hash, allow_passwordless_login "
            "FROM flr_sample_users WHERE user_id = 'user_default'"
        ).fetchone()
        with pytest.raises(AuthError, match="Invalid credentials"):
            login(migrated.conn, user_id="user_default", password="admin")

    assert tuple(row) == (0, None, 0)
