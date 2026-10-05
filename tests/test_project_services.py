"""Standalone immutable project-version and publication contract."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier

import pytest
from mmfdb.project import services
from mmfdb.repository import MFDatabase
from mmfdb.security.auth import PERM_WRITE, create_session, grant_acl, principal_from_rpc_auth


@pytest.fixture
def project_db(tmp_path, monkeypatch):
    """Provide isolated storage and two authenticated owners."""
    monkeypatch.setenv("MMFDB_OBJECT_STORE_ROOT", str(tmp_path / "objects"))
    path = tmp_path / "projects.sqlite"
    with MFDatabase(path) as db:
        auth = {}
        for user in ("owner", "other"):
            db.add_user(user, user)
            auth[user] = {"token": create_session(db.conn, user)["token"]}
        db.conn.commit()
    return path, auth


@pytest.fixture
def payload():
    """Use a v5 JSON envelope without importing its scientific producer."""
    return {
        "project_format_version": 5,
        "meta": {"name": "Scientific snapshot"},
        "datasets": {}, "experiments": {}, "fits": [], "ui": {},
        "extra": {"label": "original"}, "dependency_edges": [], "parameters": {},
    }


def _save(project_db, payload, *, user="owner", **kwargs):
    """Call the real standalone service with a persisted session."""
    path, auth = project_db
    return services.save_project_handler(db_path=str(path), auth=auth[user],
                                         project_payload=payload, **kwargs)


def _rows(path, table):
    """Inspect committed records on a fresh connection."""
    with MFDatabase(path) as db:
        return [dict(row) for row in db.conn.execute(f"SELECT * FROM {table}").fetchall()]


def test_extend_without_parent_allocates_next_number_and_links_latest(project_db, payload):
    first = _save(project_db, payload)
    second = _save(project_db, payload, project_id=first["project_id"])
    assert second["ok"], second
    assert second["version_number"] == 2
    assert second["parent_version_id"] == first["version_id"]


def test_older_parent_allocates_after_project_maximum(project_db, payload):
    first = _save(project_db, payload)
    second = _save(project_db, payload, project_id=first["project_id"],
                   parent_version_id=first["version_id"])
    third = _save(project_db, payload, project_id=first["project_id"],
                  parent_version_id=first["version_id"])
    assert second["ok"] and third["ok"]
    assert third["version_number"] == 3
    assert third["parent_version_id"] == first["version_id"]


def test_parent_alone_extends_its_project(project_db, payload):
    first = _save(project_db, payload)
    second = _save(project_db, payload, parent_version_id=first["version_id"])
    assert second["ok"], second
    assert second["project_id"] == first["project_id"]
    assert second["version_number"] == 2


@pytest.mark.parametrize("with_parent", [False, True])
def test_nonowner_cannot_append_even_with_write_grant(project_db, payload, with_parent):
    first = _save(project_db, payload)
    path, auth = project_db
    with MFDatabase(path) as db:
        principal = principal_from_rpc_auth(db.conn, auth["owner"])
        grant_acl(db.conn, principal, "operation", first["version_id"],
                  "user", "other", PERM_WRITE)
        db.conn.commit()
    before = _rows(path, "mmfdb_operation")
    result = _save(project_db, payload, user="other", project_id=first["project_id"],
                   **({"parent_version_id": first["version_id"]} if with_parent else {}))
    assert result["ok"] is False, result
    assert _rows(path, "mmfdb_operation") == before


def test_owner_requires_write_access_without_parent(project_db, payload):
    first = _save(project_db, payload)
    path, _ = project_db
    with MFDatabase(path) as db:
        db.conn.execute("UPDATE mmfdb_object_acl SET mode = ? WHERE object_id = ?",
                        (0o400, first["version_id"]))
        db.conn.commit()
    result = _save(project_db, payload, project_id=first["project_id"])
    assert result["ok"] is False, result


@pytest.mark.parametrize("bad_parent", ["missing", "different-project"])
def test_parent_failure_publishes_no_branch_or_version(project_db, payload, bad_parent):
    first = _save(project_db, payload)
    path, _ = project_db
    before_branches = _rows(path, "mmfdb_branch")
    before_versions = _rows(path, "mmfdb_operation")
    result = _save(project_db, payload, project_id="unused-project",
                   parent_version_id="missing" if bad_parent == "missing" else first["version_id"])
    assert result["ok"] is False
    assert _rows(path, "mmfdb_branch") == before_branches
    assert _rows(path, "mmfdb_operation") == before_versions


def test_failed_publication_rolls_back_snapshot_branch_and_acl(project_db, payload, monkeypatch):
    path, _ = project_db
    tables = ("mmfdb_operation", "mmfdb_branch", "mmfdb_object_acl", "mmfdb_audit_log")
    before = {table: _rows(path, table) for table in tables}
    original = MFDatabase.add_audit_log

    def fail_project_audit(self, *args, **kwargs):
        """Fail after the archive and ACL writes, before publication."""
        if kwargs.get("target_type") == "project":
            raise RuntimeError("publication failed")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(MFDatabase, "add_audit_log", fail_project_audit)
    result = _save(project_db, payload, visibility="public")
    assert result["ok"] is False
    assert "publication failed" in result["error"]
    assert {table: _rows(path, table) for table in tables} == before


def test_deleted_version_numbers_are_not_reused(project_db, payload):
    first = _save(project_db, payload)
    second = _save(project_db, payload, project_id=first["project_id"],
                   parent_version_id=first["version_id"])
    path, _ = project_db
    with MFDatabase(path) as db:
        db.conn.execute("UPDATE mmfdb_operation SET deleted_at = ? WHERE operation_id = ?",
                        ("2026-10-03", second["version_id"]))
        db.conn.commit()
    third = _save(project_db, payload, project_id=first["project_id"])
    assert third["ok"], third
    assert third["version_number"] == 3
    assert third["parent_version_id"] == first["version_id"]


def test_concurrent_saves_allocate_distinct_sequential_versions(project_db, payload, monkeypatch):
    first = _save(project_db, payload)
    barrier = Barrier(4)
    original = services._auth_db

    def synchronized_auth(*args, **kwargs):
        """Start allocation only once all connections have authenticated."""
        result = original(*args, **kwargs)
        barrier.wait(timeout=10)
        return result

    monkeypatch.setattr(services, "_auth_db", synchronized_auth)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: _save(project_db, payload,
                            project_id=first["project_id"], parent_version_id=first["version_id"]),
                            range(4)))
    assert all(result["ok"] for result in results), results
    assert sorted(result["version_number"] for result in results) == [2, 3, 4, 5]
    assert len({result["version_id"] for result in results}) == 4
    assert len(_rows(project_db[0], "mmfdb_operation")) == 5


@pytest.mark.parametrize("version", [None, 4, 6, "5", True])
def test_save_rejects_noncanonical_format_without_writes(project_db, payload, version):
    if version is None:
        payload.pop("project_format_version")
    else:
        payload["project_format_version"] = version
    path, _ = project_db
    before = _rows(path, "mmfdb_branch")
    result = _save(project_db, payload)
    assert result["ok"] is False, result
    assert result["error_code"] == "INVALID_INPUT"
    assert _rows(path, "mmfdb_operation") == []
    assert _rows(path, "mmfdb_branch") == before


def test_restore_rejects_obsolete_stored_snapshot(project_db, payload):
    first = _save(project_db, payload)
    path, auth = project_db
    with MFDatabase(path) as db:
        db.conn.execute("UPDATE mmfdb_operation SET metadata_json = "
                        "json_set(metadata_json, '$.fit_structure.project_format_version', 4) "
                        "WHERE operation_id = ?", (first["version_id"],))
        db.conn.commit()
    result = services.restore_project_handler(db_path=str(path), auth=auth["owner"],
                                               version_id=first["version_id"])
    assert result["ok"] is False, result


def test_exact_payload_readback_and_immutable_existing_record(project_db, payload):
    path, auth = project_db
    first_payload = deepcopy(payload)
    first = _save(project_db, first_payload, visibility="public")
    before = _rows(path, "mmfdb_operation")[0]
    payload["extra"]["label"] = "changed"
    second = _save(project_db, payload, project_id=first["project_id"])
    assert second["ok"], second
    for version, expected in ((first, first_payload), (second, payload)):
        restored = services.restore_project_handler(db_path=str(path), auth=auth["owner"],
                                                     version_id=version["version_id"])
        assert restored["ok"] and restored["project_payload"] == expected
    assert _rows(path, "mmfdb_operation")[0] == before
    acl = next(row for row in _rows(path, "mmfdb_object_acl")
               if row["object_id"] == second["version_id"])
    assert acl["owner_user_id"] == "owner"
    assert acl["mode"] & 0o7 == 0


def test_other_projects_branch_cannot_be_retargeted(project_db, payload):
    first = _save(project_db, payload)
    path, _ = project_db
    before = _rows(path, "mmfdb_branch")
    result = _save(project_db, payload, branch_uuid=first["branch_uuid"])
    assert result["ok"] is False, result
    assert _rows(path, "mmfdb_branch") == before


def test_failed_branch_head_update_is_not_success(project_db, payload, monkeypatch):
    path, _ = project_db
    before = _rows(path, "mmfdb_branch")

    def fail_head(*_args, **_kwargs):
        """Simulate the archiver's caught branch-publication failure."""
        raise RuntimeError("branch head failure")

    monkeypatch.setattr(MFDatabase, "update_branch_head", fail_head)
    result = _save(project_db, payload)
    assert result["ok"] is False, result
    assert _rows(path, "mmfdb_operation") == []
    assert _rows(path, "mmfdb_branch") == before


def test_branch_and_version_are_owned_and_published_together(project_db, payload):
    result = _save(project_db, payload)
    path, _ = project_db
    branch = next(row for row in _rows(path, "mmfdb_branch")
                  if row["branch_uuid"] == result["branch_uuid"])
    assert branch["head_operation_id"] == result["version_id"]
    acl = next(row for row in _rows(path, "mmfdb_object_acl")
               if row["object_type"] == "branch" and row["object_id"] == result["branch_uuid"])
    assert acl["owner_user_id"] == "owner"


@pytest.mark.parametrize("section,value", [("fits", {}), ("datasets", []), ("meta", []),
                                           ("extra", []), ("parameters", []), ("ui", [])])
def test_save_rejects_malformed_envelope(project_db, payload, section, value):
    payload[section] = value
    result = _save(project_db, payload)
    assert result["ok"] is False, result
    assert result["error_code"] == "INVALID_INPUT"
    assert _rows(project_db[0], "mmfdb_operation") == []


def test_failed_authentication_closes_request_connection(project_db, payload, monkeypatch):
    path, _ = project_db
    opened = []
    original = services._get_db

    def track_open(*args, **kwargs):
        """Keep the real request connection visible after the handler returns."""
        db = original(*args, **kwargs)
        opened.append(db)
        return db

    monkeypatch.setattr(services, "_get_db", track_open)
    result = services.save_project_handler(db_path=str(path), auth={"token": "invalid"},
                                           project_payload=payload)
    assert result["ok"] is False
    assert opened[0]._conn is None


def test_deleted_project_cannot_be_claimed_by_another_owner(project_db, payload):
    first = _save(project_db, payload)
    path, _ = project_db
    with MFDatabase(path) as db:
        db.conn.execute("UPDATE mmfdb_operation SET deleted_at = ? WHERE operation_id = ?",
                        ("2026-10-03", first["version_id"]))
        db.conn.commit()
    result = _save(project_db, payload, user="other", project_id=first["project_id"])
    assert result["ok"] is False
    assert len(_rows(path, "mmfdb_operation")) == 1


def test_concurrent_initial_saves_serialize_creation_and_allocation(project_db, payload, monkeypatch):
    barrier = Barrier(4)
    original = services._auth_db

    def synchronized_auth(*args, **kwargs):
        """Align requests before the first project identity exists."""
        result = original(*args, **kwargs)
        barrier.wait(timeout=10)
        return result

    monkeypatch.setattr(services, "_auth_db", synchronized_auth)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: _save(project_db, payload, project_id="same-new-project"),
                                range(4)))
    assert all(result["ok"] for result in results), results
    assert sorted(result["version_number"] for result in results) == [1, 2, 3, 4]
    assert len({result["branch_uuid"] for result in results}) == 1
    by_number = {result["version_number"]: result for result in results}
    for number in range(2, 5):
        assert by_number[number]["parent_version_id"] == by_number[number - 1]["version_id"]


def test_failed_publication_compensates_real_dataset_objects(project_db, payload, monkeypatch):
    from mmfdb.store.database_resolver import object_store_root

    path, _ = project_db
    payload["datasets"] = {"curve": {"name": "curve", "x": [0.0, 1.0], "y": [2.0, 3.0]}}
    root = object_store_root()
    files_before = {file for file in root.rglob("*") if file.is_file()}
    tables = ("mmfdb_operation", "mmfdb_branch", "mmfdb_object_acl", "mmfdb_artifact", "mmfdb_object")
    before = {table: _rows(path, table) for table in tables}
    original = MFDatabase.add_audit_log
    created_files = []

    def fail_after_objects(self, *args, **kwargs):
        """Observe real object writes, then reject publication."""
        if kwargs.get("target_type") == "project":
            created_files.extend(file for file in root.rglob("*")
                                 if file.is_file() and file not in files_before)
            raise RuntimeError("publication failed after objects")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(MFDatabase, "add_audit_log", fail_after_objects)
    result = _save(project_db, payload)
    assert result["ok"] is False
    assert created_files, "Test must exercise real object-store writes"
    assert {table: _rows(path, table) for table in tables} == before
    assert {file for file in root.rglob("*") if file.is_file()} == files_before


def test_implicit_extension_keeps_its_parent_branch(project_db, payload):
    path, _ = project_db
    with MFDatabase(path) as db:
        branch_uuid = db.create_branch(name="scientific-work", created_by_user_id="owner")
    first = _save(project_db, payload, branch_uuid=branch_uuid)
    assert first["ok"], first
    second = _save(project_db, payload, project_id=first["project_id"])
    assert second["ok"], second
    assert second["branch_uuid"] == branch_uuid
    branch = next(row for row in _rows(path, "mmfdb_branch") if row["branch_uuid"] == branch_uuid)
    assert branch["head_operation_id"] == second["version_id"]
