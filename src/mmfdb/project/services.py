"""Canonical project snapshot services for standalone MMFDB deployments.

The project payload is stored on the ``project`` operation itself.  Artifact
and provenance indexing remains the responsibility of the archiver, but
restore never reconstructs a snapshot from those lossy indexes.
"""

from __future__ import annotations

import base64
import json
import uuid
from typing import Any, cast

from mmfdb.admin.backend._service_errors import (
    INVALID_INPUT,
    NOT_FOUND,
    OPERATION_FAILED,
    service_error,
)
from mmfdb.project.project_archiver import archive_project_to_mmfdb
from mmfdb.repository import MFDatabase
from mmfdb.schema._sqlutil import _json_loads, _utc_now
from mmfdb.security.auth import (
    PERM_MANAGE,
    PERM_READ,
    PERM_WRITE,
    chmod,
    filter_readable,
    principal_from_rpc_auth,
    require_access,
    require_authenticated,
)
from mmfdb.store.sql_backend import connection_dialect


def _get_db(db_path: str = "") -> MFDatabase:
    """Open the configured database, allowing tests to inject a path."""
    if db_path:
        return MFDatabase(db_path)
    from mmfdb.store import database_resolver

    return MFDatabase(str(database_resolver.resolve_database_path()))


def _auth_db(auth: dict[str, Any] | None, db_path: str) -> tuple[Any, Any, MFDatabase]:
    """Authenticate a request, closing its connection on rejected credentials."""
    db = _get_db(db_path)
    try:
        principal = principal_from_rpc_auth(db.conn, auth)
        require_authenticated(principal)
        return principal, db.conn, db
    except Exception:
        db.close()
        raise


def _meta(row: Any) -> dict[str, Any]:
    return _json_loads(row["metadata_json"]) or {}


def _counts(payload: Any) -> tuple[int, int]:
    if not isinstance(payload, dict):
        return 0, 0
    datasets = payload.get("datasets") or {}
    fits = payload.get("fits") or []
    return len(fits), len(datasets)


def _visibility(conn: Any, version_id: str) -> str:
    row = conn.execute(
        "SELECT mode FROM mmfdb_object_acl WHERE object_type = 'operation' "
        "AND object_id = ? AND deleted_at IS NULL", (version_id,)
    ).fetchone()
    mode = row[0] if row else 0
    if mode & 0o7 & PERM_READ:
        return "public"
    entries = conn.execute(
        "SELECT 1 FROM mmfdb_acl_entry WHERE object_type = 'operation' "
        "AND object_id = ? AND deleted_at IS NULL LIMIT 1", (version_id,)
    ).fetchone()
    return "shared" if entries else "private"


def _version(conn: Any, version_id: str) -> Any:
    return conn.execute(
        "SELECT * FROM mmfdb_operation WHERE operation_id = ? "
        "AND operation_type = 'project' AND deleted_at IS NULL", (version_id,)
    ).fetchone()


def _payload_error(payload: Any) -> str | None:
    """Validate the v5 transport envelope independently of its scientific producer."""
    if not isinstance(payload, dict):
        return "project_payload must be an object"
    version = payload.get("project_format_version")
    if type(version) is not int or version != 5:
        return "project_payload requires project format v5"
    for section, expected in {
        "meta": dict, "datasets": dict, "experiments": dict, "fits": list,
        "ui": dict, "extra": dict, "dependency_edges": list, "parameters": dict,
    }.items():
        if section in payload and not isinstance(payload[section], expected):
            return f"Project section {section!r} must be a {expected.__name__}"
    return None


def save_project_handler(*, db_path: str = "", auth: dict[str, Any] | None = None,
                         project_name: str | None = None,
                         project_payload: dict[str, Any] | None = None,
                         project_id: str | None = None,
                         parent_version_id: str | None = None,
                         notes: str | None = None,
                         visibility: str = "private", fit_count: int = 0,
                         dataset_count: int = 0, branch_uuid: str | None = None,
                         resource_bundle: dict[str, str] | None = None) -> dict[str, Any]:
    """Authorize, allocate and publish one immutable project version atomically."""
    try:
        principal, conn, db = _auth_db(auth, db_path)
        if visibility not in {"private", "public"}:
            return service_error("visibility must be 'private' or 'public'", error_code=INVALID_INPUT)
        error = _payload_error(project_payload)
        if error:
            return service_error(error, error_code=INVALID_INPUT)
        assert isinstance(project_payload, dict)
        resources = _decode_resources(resource_bundle or {})
        user_id = principal.user_id
        version_id = f"ver_{uuid.uuid4().hex}"

        with db.transaction():
            if connection_dialect(conn) == "sqlite":
                # Acquire SQLite's writer reservation before any allocation read.
                # An empty UPDATE locks without altering existing version records;
                # the repository transaction owns commit and rollback compensation.
                conn.execute("UPDATE mmfdb_operation SET operation_id = operation_id WHERE 0")
            else:
                cursor = conn.execute("LOCK TABLE mmfdb_operation IN SHARE ROW EXCLUSIVE MODE")
                cursor.close()

            if parent_version_id:
                parent = _version(conn, parent_version_id)
                if parent is None:
                    return service_error("parent project version not found", error_code=NOT_FOUND)
                require_access(conn, principal, "operation", parent_version_id, PERM_WRITE)
                parent_project_id = _meta(parent).get("project_id")
                if project_id and parent_project_id != project_id:
                    return service_error("parent belongs to a different project", error_code=INVALID_INPUT)
                project_id = parent_project_id
                if not project_id:
                    return service_error("parent has no project identity", error_code=INVALID_INPUT)

            project_id = project_id or f"proj_{uuid.uuid4().hex}"
            # Deleted records still reserve their identity and version number.
            versions = conn.execute(
                "SELECT * FROM mmfdb_operation WHERE operation_type = 'project' "
                "AND json_extract(metadata_json, '$.project_id') = ?", (project_id,),
            ).fetchall()
            version_number = 1
            if versions:
                if any(row["operator_user_id"] != user_id for row in versions):
                    return service_error("only the project owner may extend a project", error_code=OPERATION_FAILED)
                numbered = [(row, _meta(row).get("version_number")) for row in versions]
                if any(type(number) is not int or number < 1 for _, number in numbered):
                    return service_error("project has invalid version numbers", error_code=INVALID_INPUT)
                checked_numbers = [(row, cast(int, number)) for row, number in numbered]
                version_number = max(number for _, number in checked_numbers) + 1
                active = [(row, number) for row, number in checked_numbers if not row["deleted_at"]]
                if not active:
                    return service_error("project has no active version to extend", error_code=NOT_FOUND)
                latest_number = max(number for _, number in active)
                latest = [row for row, number in active if number == latest_number]
                for row in latest:
                    require_access(conn, principal, "operation", row["operation_id"], PERM_WRITE)
                if not parent_version_id:
                    if len(latest) != 1:
                        return service_error("project has an ambiguous latest version", error_code=INVALID_INPUT)
                    parent = latest[0]
                    parent_version_id = parent["operation_id"]

            if not branch_uuid and parent_version_id:
                branch_uuid = _meta(parent).get("branch_uuid")
            if not branch_uuid:
                branch_name = f"project_{project_id}"
                branch = conn.execute(
                    "SELECT branch_uuid FROM mmfdb_branch WHERE name = ? AND deleted_at IS NULL",
                    (branch_name,),
                ).fetchone()
                branch_uuid = branch[0] if branch else db.create_branch(
                    branch_uuid=f"br_{uuid.uuid4().hex}", name=branch_name,
                    description=f"Default branch for {project_id}", created_by_user_id=user_id,
                )
            branch = conn.execute(
                "SELECT * FROM mmfdb_branch WHERE branch_uuid = ? AND deleted_at IS NULL",
                (branch_uuid,),
            ).fetchone()
            if branch is None:
                raise ValueError("project branch not found")
            require_access(conn, principal, "branch", branch_uuid, PERM_WRITE)
            if branch["created_by_user_id"] != user_id:
                raise PermissionError("only the branch owner may publish a project")
            if branch["head_operation_id"]:
                head = conn.execute(
                    "SELECT * FROM mmfdb_operation WHERE operation_id = ?",
                    (branch["head_operation_id"],),
                ).fetchone()
                if head is None or _meta(head).get("project_id") != project_id:
                    raise ValueError("branch belongs to a different project")

            if parent_version_id:
                labels = _resource_labels(project_payload)
                inherited = [artifact for artifact in map(dict, db.get_operation_artifacts(parent_version_id))
                             if artifact.get("artifact_kind") == "raw_measurement"
                             and artifact.get("file_path") in labels
                             and artifact.get("file_path") not in resources]
                if inherited:
                    require_access(conn, principal, "operation", parent_version_id, PERM_READ)
                for artifact in inherited:
                    resources[artifact["file_path"]] = db.get_object(artifact["object_uuid"])

            result = archive_project_to_mmfdb(
                db=db, project_payload=project_payload, version_id=version_id,
                project_id=project_id, version_number=version_number,
                parent_version_id=parent_version_id, branch_uuid=branch_uuid,
                user_id=user_id, notes=notes or "", project_name=project_name or "",
                resource_bundle=resources,
            )
            head = conn.execute(
                "SELECT head_operation_id FROM mmfdb_branch WHERE branch_uuid = ?", (branch_uuid,),
            ).fetchone()
            if head is None or head[0] != version_id:
                raise RuntimeError("project branch head publication failed")
            if visibility == "public":
                chmod(conn, principal, "operation", version_id, 0o704)
            db.add_audit_log(
                action="archive", target_type="project", target_id=version_id,
                operator_user_id=user_id,
                details={"project_id": project_id, "version_number": version_number},
            )
        return {
            "ok": True, "project_id": project_id, "version_id": version_id,
            "version_number": version_number, "parent_version_id": parent_version_id,
            "branch_uuid": branch_uuid, "project_name": project_name or "",
            "visibility": visibility, "project_payload": project_payload,
            "artifact_count": len(result.get("dataset_artifacts", [])) + len(result.get("fit_artifacts", [])),
            "parameter_count": result.get("parameter_count", 0),
            "edge_count": result.get("edge_count", 0),
        }
    except Exception as exc:  # noqa: BLE001 - RPC boundary returns explicit failure
        return service_error(str(exc), error_code=OPERATION_FAILED, exception=exc)
    finally:
        if "db" in locals():
            db.close()


def restore_project_handler(*, db_path: str = "", auth: dict[str, Any] | None = None,
                            version_id: str | None = None) -> dict[str, Any]:
    """Restore the exact JSON snapshot stored on a project operation."""
    try:
        principal, conn, db = _auth_db(auth, db_path)
        if version_id is None:
            row = conn.execute(
                "SELECT operation_id FROM mmfdb_operation WHERE operation_type = 'project' "
                "AND deleted_at IS NULL ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            version_id = row[0] if row else None
        if not version_id:
            return service_error("No project found", error_code=NOT_FOUND)
        require_access(conn, principal, "operation", version_id, PERM_READ)
        row = _version(conn, version_id)
        if row is None:
            return service_error("Project version not found", error_code=NOT_FOUND)
        meta = _meta(row)
        payload = meta.get("fit_structure")
        error = _payload_error(payload)
        if error:
            return service_error(error, error_code=INVALID_INPUT)
        db.add_audit_log(
            action="restore", target_type="project", target_id=version_id,
            operator_user_id=principal.user_id,
            details={"project_id": meta.get("project_id")},
        )
        return {
            "ok": True, "version_id": version_id, "project_id": meta.get("project_id", version_id),
            "version_number": meta.get("version_number", 1),
            "parent_version_id": meta.get("parent_version_id"),
            "branch_uuid": meta.get("branch_uuid"),
            "project_name": meta.get("project_name", meta.get("model_name", "")),
            "project_payload": payload, "visibility": _visibility(conn, version_id),
        }
    except Exception as exc:  # noqa: BLE001 - RPC boundary returns explicit failure
        return service_error(str(exc), error_code=OPERATION_FAILED, exception=exc)
    finally:
        if "db" in locals():
            db.close()


def list_projects_handler(*, db_path: str = "", auth: dict[str, Any] | None = None,
                          show_public: bool = True, search: str | None = None) -> dict[str, Any]:
    """List readable canonical project versions grouped by project ID."""
    try:
        principal, conn, _db = _auth_db(auth, db_path)
        rows = conn.execute(
            "SELECT * FROM mmfdb_operation WHERE operation_type = 'project' "
            "AND deleted_at IS NULL ORDER BY created_at DESC"
        ).fetchall()
        if not principal.is_admin:
            rows = filter_readable(conn, principal, "operation", rows, id_key="operation_id")
        if not show_public:
            rows = [row for row in rows if _visibility(conn, row["operation_id"]) != "public"]
        grouped: dict[str, dict[str, Any]] = {}
        for row in rows:
            meta = _meta(row)
            payload = meta.get("fit_structure")
            fits, datasets = _counts(payload)
            pid = meta.get("project_id", row["operation_id"])
            if search and search.lower() not in json_search(meta, row).lower():
                continue
            group = grouped.setdefault(pid, {
                "project_id": pid, "project_name": meta.get("project_name", meta.get("model_name", "")),
                "owner_user_id": row["operator_user_id"], "versions": [], "version_count": 0,
                "latest_version_number": 0, "latest_version_id": "", "visibility": "private",
                "created_at": row["created_at"], "updated_at": row["created_at"],
            })
            version = {"version_id": row["operation_id"], "project_name": group["project_name"],
                       "project_id": pid, "version_number": meta.get("version_number", 1),
                       "parent_version_id": meta.get("parent_version_id"), "owner_user_id": row["operator_user_id"],
                       "status": row["status"], "notes": meta.get("notes", ""), "fit_count": meta.get("fit_count", fits),
                       "dataset_count": meta.get("dataset_count", datasets), "created_at": row["created_at"],
                       "updated_at": row["updated_at"]}
            group["versions"].append(version)
            group["version_count"] += 1
            if version["version_number"] > group["latest_version_number"]:
                group["latest_version_number"] = version["version_number"]
                group["latest_version_id"] = row["operation_id"]
                group["visibility"] = _visibility(conn, row["operation_id"])
        return {"ok": True, "projects": list(grouped.values())}
    except Exception as exc:  # noqa: BLE001 - RPC boundary returns explicit failure
        return service_error(str(exc), error_code=OPERATION_FAILED, exception=exc)
    finally:
        if "_db" in locals():
            _db.close()


def json_search(meta: dict[str, Any], row: Any) -> str:
    """Build the searchable text without coupling to a client application."""
    return " ".join(str(value) for value in (meta.get("project_id", ""), meta.get("project_name", ""), meta.get("notes", ""), row["operator_user_id"]))


def delete_version_handler(
    auth: dict[str, Any] | None = None,
    db_path: str = "",
    version_id: str | None = None,
) -> dict[str, Any]:
    try:
        principal, conn, db = _auth_db(auth, db_path)
        if not version_id:
            return service_error("version_id is required", error_code=INVALID_INPUT)
        if _version(conn, version_id) is None:
            return service_error('Project version not found', error_code=NOT_FOUND)
        require_access(conn, principal, "operation", version_id, PERM_MANAGE)
        with db.transaction():
            conn.execute(
                "UPDATE mmfdb_operation SET deleted_at = ? WHERE operation_id = ?",
                (_utc_now(), version_id),
            )
            conn.execute(
                "UPDATE mmfdb_object_acl SET deleted_at = ? WHERE object_type = 'operation' AND object_id = ?",
                (_utc_now(), version_id),
            )
            db.add_audit_log(
                action="delete",
                target_type="project_version",
                target_id=version_id,
                details={"user_id": principal.user_id},
            )
        return {"ok": True, "deleted_version_id": version_id}
    except Exception as exc:  # noqa: BLE001 - RPC boundary returns explicit failure
        return service_error(str(exc), error_code=OPERATION_FAILED, exception=exc)
    finally:
        if "db" in locals():
            db.close()


# ── Branch Management ──────────────────────────────────────────────────


def create_branch_handler(
    auth: dict[str, Any] | None = None,
    db_path: str = "",
    project_id: str | None = None,
    from_version_id: str | None = None,
    branch_name: str | None = None,
) -> dict[str, Any]:
    """Create a new branch from an existing version.

    Parameters
    ----------
    auth : dict
        Authentication context.
    project_id : str
        Project identifier.
    from_version_id : str
        Version ID to fork from (becomes the branch head).
    branch_name : str
        Human-readable branch name.

    Returns
    -------
    dict
        ``{ok, branch_uuid, branch_name, head_version_id}``.
    """
    try:
        principal, conn, db = _auth_db(auth, db_path)
        user_id = principal.user_id or "user_default"

        if not project_id or not from_version_id or not branch_name:
            return service_error(
                "project_id, from_version_id, and branch_name are required",
                error_code=INVALID_INPUT,
            )

        source = _version(conn, from_version_id)
        if source is None or _meta(source).get("project_id") != project_id:
            raise ValueError("Branch source does not belong to this project")
        require_access(conn, principal, "operation", from_version_id, PERM_READ)

        import uuid as _uuid

        branch_uuid = f"br_{_uuid.uuid4().hex[:12]}"

        with db.transaction():
            conn.execute(
                """INSERT INTO mmfdb_branch (branch_uuid, name, description, head_operation_id, created_by_user_id)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    branch_uuid,
                    branch_name,
                    f"Forked from {from_version_id}",
                    from_version_id,
                    user_id,
                ),
            )
            db.add_audit_log(
                action="create_branch",
                target_type="branch",
                target_id=branch_uuid,
                details={
                    "project_id": project_id,
                    "from_version_id": from_version_id,
                    "branch_name": branch_name,
                    "user_id": user_id,
                },
            )

        return {
            "ok": True,
            "branch_uuid": branch_uuid,
            "branch_name": branch_name,
            "head_version_id": from_version_id,
        }
    except Exception as exc:  # noqa: BLE001 - RPC boundary returns explicit failure
        return service_error(str(exc), error_code=OPERATION_FAILED, exception=exc)
    finally:
        if "db" in locals():
            db.close()


def list_branches_handler(auth: dict[str, Any] | None = None, db_path: str = "",
                          project_id: str | None = None) -> dict[str, Any]:
    """List branches and counts exclusively from readable project versions."""
    try:
        principal, conn, db = _auth_db(auth, db_path)
        if not project_id:
            raise ValueError("project_id is required")
        rows = conn.execute(
            "SELECT * FROM mmfdb_operation WHERE operation_type = 'project' AND deleted_at IS NULL "
            "AND json_extract(metadata_json, '$.project_id') = ?", (project_id,),
        ).fetchall()
        rows = filter_readable(conn, principal, "operation", rows, id_key="operation_id")
        branches: dict[str, dict[str, Any]] = {}
        for row in rows:
            meta = _meta(row)
            branch_id = meta.get("branch_uuid")
            if not isinstance(branch_id, str):
                continue
            branch = conn.execute("SELECT name, head_operation_id FROM mmfdb_branch WHERE branch_uuid = ? "
                                  "AND deleted_at IS NULL", (branch_id,)).fetchone()
            if branch:
                item = branches.setdefault(branch_id, {"branch_uuid": branch_id, "name": branch[0],
                                                       "head_version_id": None, "version_count": 0})
                item["version_count"] += 1
                if branch[1] == row["operation_id"]:
                    item["head_version_id"] = branch[1]
        return {"ok": True, "branches": list(branches.values())}
    except Exception as exc:  # noqa: BLE001 - RPC boundary returns explicit failure
        return service_error(str(exc), error_code=OPERATION_FAILED, exception=exc)
    finally:
        if "db" in locals():
            db.close()


def get_version_graph_handler(
    auth: dict[str, Any] | None = None,
    db_path: str = "",
    project_id: str | None = None,
) -> dict[str, Any]:
    """Get the full version DAG for a project.

    Parameters
    ----------
    auth : dict
        Authentication context.
    project_id : str
        Project identifier.

    Returns
    -------
    dict
        ``{ok, graph: {nodes: [...], edges: [...], roots: [...], leaves: [...]}}``.
    """
    try:
        principal, conn, db = _auth_db(auth, db_path)

        if not project_id:
            return service_error("project_id is required", error_code=INVALID_INPUT)

        # Get all versions for this project
        rows = conn.execute(
            """SELECT *
               FROM mmfdb_operation
               WHERE operation_type = 'project' AND deleted_at IS NULL
                 AND json_extract(metadata_json, '$.project_id') = ?
               ORDER BY created_at""",
            (project_id,),
        ).fetchall()

        rows = filter_readable(conn, principal, "operation", rows, id_key="operation_id")
        nodes = []
        node_ids = set()
        for row in rows:
            op_id = row["operation_id"] if isinstance(row, dict) else row[0]
            meta_raw = row["metadata_json"] if isinstance(row, dict) else row[1]
            created = row["created_at"] if isinstance(row, dict) else row[2]
            meta = _json_loads(meta_raw) if isinstance(meta_raw, str) else (meta_raw or {})

            node_ids.add(op_id)
            nodes.append(
                {
                    "version_id": op_id,
                    "version_number": meta.get("version_number", 0),
                    "branch_uuid": meta.get("branch_uuid"),
                    "project_name": meta.get("project_name", ""),
                    "notes": meta.get("notes", ""),
                    "fit_count": meta.get("fit_count", 0),
                    "dataset_count": meta.get("dataset_count", 0),
                    "created_at": created,
                }
            )

        # Build edges from parent_version_id metadata
        edges = []
        for node in nodes:
            parent_id = None
            # Find parent from metadata
            for row in rows:
                op_id = row["operation_id"] if isinstance(row, dict) else row[0]
                if op_id == node["version_id"]:
                    meta_raw = row["metadata_json"] if isinstance(row, dict) else row[1]
                    meta = _json_loads(meta_raw) if isinstance(meta_raw, str) else (meta_raw or {})
                    parent_id = meta.get("parent_version_id")
                    break
            if parent_id and parent_id in node_ids:
                edges.append(
                    {
                        "source": node["version_id"],
                        "target": parent_id,
                        "relationship": "supersedes",
                    }
                )

        # Also query mmfdb_edge for supersedes edges. A supersedes edge is stored
        # as newer_version -> parent_version.
        edge_rows = []
        if node_ids:
            edge_rows = conn.execute(
                """SELECT source_node_id, target_node_id, metadata_json
                   FROM mmfdb_edge
                   WHERE relationship_type = 'supersedes' AND deleted_at IS NULL
                     AND source_node_id IN ({})""".format(",".join("?" * len(node_ids))),
                list(node_ids),
            ).fetchall()
        existing_edge_keys = {(e["source"], e["target"]) for e in edges}
        for erow in edge_rows:
            src = erow["source_node_id"] if isinstance(erow, dict) else erow[0]
            tgt = erow["target_node_id"] if isinstance(erow, dict) else erow[1]
            if src in node_ids and tgt in node_ids and (src, tgt) not in existing_edge_keys:
                edges.append({"source": src, "target": tgt, "relationship": "supersedes"})
                existing_edge_keys.add((src, tgt))

        # Identify roots (no parent) and leaves (no children). With the stored
        # edge direction newer -> parent, nodes appearing as edge sources have a
        # parent and nodes appearing as edge targets have at least one child.
        nodes_with_parent = {e["source"] for e in edges}
        nodes_with_child = {e["target"] for e in edges}
        roots = [n for n in nodes if n["version_id"] not in nodes_with_parent]
        leaves = [n for n in nodes if n["version_id"] not in nodes_with_child]

        return {
            "ok": True,
            "graph": {
                "nodes": nodes,
                "edges": edges,
                "roots": [r["version_id"] for r in roots],
                "leaves": [l["version_id"] for l in leaves],
            },
        }
    except Exception as exc:  # noqa: BLE001 - RPC boundary returns explicit failure
        return service_error(str(exc), error_code=OPERATION_FAILED, exception=exc)
    finally:
        if "db" in locals():
            db.close()


def list_project_artifacts_handler(
    auth: dict[str, Any] | None = None,
    db_path: str = "",
    version_id: str | None = None,
) -> dict[str, Any]:
    """List all artifacts for a project version.

    Parameters
    ----------
    auth : dict
        Authentication context.
    version_id : str
        Version identifier.

    Returns
    -------
    dict
        ``{ok, artifacts: [...]}``.
    """
    try:
        principal, conn, db = _auth_db(auth, db_path)
        if not version_id:
            return service_error("version_id is required", error_code=INVALID_INPUT)
        require_access(conn, principal, "operation", version_id, PERM_READ)

        artifacts_by_id = {}
        for art_row in db.get_operation_artifacts(version_id):
            art = dict(art_row)
            artifact_id = art.get("artifact_id")
            if artifact_id:
                artifacts_by_id[artifact_id] = art
        edge_rows = conn.execute(
            """SELECT a.*, e.relationship_type
               FROM mmfdb_edge AS e
               JOIN mmfdb_artifact AS a ON a.artifact_id = e.target_node_id
               WHERE e.source_node_type = 'operation'
                 AND e.source_node_id = ?
                 AND e.target_node_type = 'artifact'
                 AND e.relationship_type = 'project_contains'
                 AND e.deleted_at IS NULL
                 AND a.deleted_at IS NULL""",
            (version_id,),
        ).fetchall()
        for row in edge_rows:
            art = dict(row)
            artifact_id = art.get("artifact_id")
            if artifact_id and artifact_id not in artifacts_by_id:
                art["role"] = art.get("role") or "project_contains"
                art["direction"] = art.get("direction") or "output"
                artifacts_by_id[artifact_id] = art

        artifacts = list(artifacts_by_id.values())
        result = []
        for art in artifacts:
            result.append(
                {
                    "artifact_id": art.get("artifact_id"),
                    "artifact_kind": art.get("artifact_kind"),
                    "role": art.get("role"),
                    "direction": art.get("direction"),
                    "storage_mode": art.get("storage_mode"),
                    "object_uuid": art.get("object_uuid"),
                    "size_bytes": art.get("size_bytes"),
                    "data_format": art.get("data_format"),
                    "file_path": art.get("file_path"),
                }
            )

        return {"ok": True, "artifacts": result}
    except Exception as exc:  # noqa: BLE001 - RPC boundary returns explicit failure
        return service_error(str(exc), error_code=OPERATION_FAILED, exception=exc)
    finally:
        if "db" in locals():
            db.close()


def list_project_parameters_handler(
    auth: dict[str, Any] | None = None,
    db_path: str = "",
    version_id: str | None = None,
) -> dict[str, Any]:
    """List all parameters for a project version (across all fits).

    Parameters
    ----------
    auth : dict
        Authentication context.
    version_id : str
        Version identifier.

    Returns
    -------
    dict
        ``{ok, parameters: [...]}``.
    """
    try:
        principal, conn, db = _auth_db(auth, db_path)
        if not version_id:
            return service_error("version_id is required", error_code=INVALID_INPUT)
        require_access(conn, principal, "operation", version_id, PERM_READ)

        # Find all fit operations that belong to this version
        fit_ops = conn.execute(
            """SELECT operation_id FROM mmfdb_operation
               WHERE (operation_id = ? OR operation_id LIKE ?) AND deleted_at IS NULL""",
            (version_id, f"fit_{version_id}:%"),
        ).fetchall()

        parameters = []
        for frow in fit_ops:
            op_id = frow["operation_id"] if isinstance(frow, dict) else frow[0]
            params = conn.execute(
                """SELECT parameter_uuid, name, value, initial_value,
                          lower_bound, upper_bound, bounds_on, parameter_type, metadata_json
                   FROM mmfdb_parameter
                   WHERE operation_id = ? AND deleted_at IS NULL""",
                (op_id,),
            ).fetchall()
            for prow in params:
                meta = (
                    _json_loads(prow["metadata_json"] if isinstance(prow, dict) else prow[8]) or {}
                )
                parameters.append(
                    {
                        "parameter_uuid": prow["parameter_uuid"]
                        if isinstance(prow, dict)
                        else prow[0],
                        "operation_id": op_id,
                        "name": prow["name"] if isinstance(prow, dict) else prow[1],
                        "value": prow["value"] if isinstance(prow, dict) else prow[2],
                        "initial_value": prow["initial_value"]
                        if isinstance(prow, dict)
                        else prow[3],
                        "lower_bound": prow["lower_bound"] if isinstance(prow, dict) else prow[4],
                        "upper_bound": prow["upper_bound"] if isinstance(prow, dict) else prow[5],
                        "bounds_on": prow["bounds_on"] if isinstance(prow, dict) else prow[6],
                        "parameter_type": prow["parameter_type"]
                        if isinstance(prow, dict)
                        else prow[7],
                        "link_target": meta.get("link_target"),
                        "fit_parameter_uid": meta.get("fit_parameter_uid"),
                    }
                )

        return {"ok": True, "parameters": parameters}
    except Exception as exc:  # noqa: BLE001 - RPC boundary returns explicit failure
        return service_error(str(exc), error_code=OPERATION_FAILED, exception=exc)
    finally:
        if "db" in locals():
            db.close()




def _decode_resources(bundle: dict[str, str]) -> dict[str, bytes]:
    """Decode content addressed by client labels; labels are never filesystem paths."""
    if not isinstance(bundle, dict):
        raise TypeError('resource_bundle must contain base64 content')
    return {name: base64.b64decode(data, validate=True) for name, data in bundle.items()}


def _dependencies(db: MFDatabase, version_id: str) -> dict[str, Any]:
    """Gather only this immutable version's indexes and attached object-store bytes."""
    conn = db.conn
    operations = [dict(row) for row in conn.execute(
        "SELECT * FROM mmfdb_operation WHERE deleted_at IS NULL AND "
        "(operation_id = ? OR operation_id LIKE ?)", (version_id, f'fit_{version_id}:%'),
    ).fetchall()]
    operation_ids = [row['operation_id'] for row in operations]
    artifacts = {}
    parameters: list[dict[str, Any]] = []
    edges = []
    for operation in operations:
        operation['metadata'] = _json_loads(operation.get('metadata_json')) or {}
        for artifact in db.get_operation_artifacts(operation['operation_id']):
            artifacts[artifact['artifact_id']] = dict(artifact)
        parameters.extend(dict(row) for row in conn.execute(
            'SELECT * FROM mmfdb_parameter WHERE operation_id = ? AND deleted_at IS NULL',
            (operation['operation_id'],),
        ).fetchall())
    for row in conn.execute(
        'SELECT * FROM mmfdb_edge WHERE deleted_at IS NULL AND source_node_id IN ({})'.format(
            ','.join('?' for _ in operation_ids)), operation_ids,
    ).fetchall():
        edges.append(dict(row))
    objects = []
    for object_uuid in sorted({art['object_uuid'] for art in artifacts.values() if art.get('object_uuid')}):
        obj = dict(db.get_object_info(object_uuid))
        obj['filename'] = obj.get('original_filename', obj.get('filename', ''))
        obj['data_base64'] = base64.b64encode(db.get_object(object_uuid)).decode('ascii')
        objects.append(obj)
    return {'operations': operations, 'artifacts': list(artifacts.values()), 'objects': objects,
            'parameters': parameters, 'provenance_edges': edges}


def export_csp_handler(*, db_path: str = '', auth: dict[str, Any] | None = None,
                       version_id: str | None = None, target_path: str | None = None) -> dict[str, Any]:
    """Export the exact authorized snapshot as typed PTO bytes, never a client path."""
    try:
        principal, conn, db = _auth_db(auth, db_path)
        if target_path is not None:
            raise ValueError('Client filesystem paths are forbidden; export returns content')
        if not version_id or _version(conn, version_id) is None:
            return service_error('Project version not found', error_code=NOT_FOUND)
        require_access(conn, principal, 'operation', version_id, PERM_READ)
        meta = _meta(_version(conn, version_id))
        payload = meta.get('fit_structure')
        error = _payload_error(payload)
        if error:
            raise ValueError(error)
        deps = _dependencies(db, version_id)
        export = {'format_version': 1, 'exported_at': _utc_now(),
                  'exported_by_user_id': principal.user_id,
                  'origin': {'project_id': meta['project_id'], 'version_id': version_id,
                             'version_number': meta['version_number'],
                             'parent_version_id': meta.get('parent_version_id')},
                  'dependencies': deps}
        from mmfdb.project.pto import write_bundle

        assert isinstance(payload, dict)
        data = write_bundle(payload, {'mmfdb_export.json': json.dumps(export, allow_nan=False).encode()})
        return {'ok': True, 'project_id': meta['project_id'], 'version_id': version_id,
                'archive_bytes': base64.b64encode(data).decode('ascii')}
    except Exception as exc:  # noqa: BLE001 - RPC boundary returns explicit failure
        return service_error(str(exc), error_code=OPERATION_FAILED, exception=exc)
    finally:
        if 'db' in locals():
            db.close()


def _import_bundle(archive_base64: str | None, file_path: str | None, data: bytes | None = None) -> tuple[dict, dict, dict]:
    """Decode a typed content bundle without opening or extracting client paths."""
    if file_path is not None:
        raise ValueError('Client filesystem paths are forbidden; supply archive content')
    if not archive_base64 and data is None:
        raise ValueError('Archive content is required')
    from mmfdb.project.pto import read_bundle

    entries = read_bundle(data if data is not None else base64.b64decode(archive_base64 or "", validate=True))
    payload = json.loads(entries['project.json'])
    error = _payload_error(payload)
    if error:
        raise ValueError(error)
    export = json.loads(entries.get('mmfdb_export.json', b'{}'))
    if not isinstance(export, dict):
        raise TypeError('Export metadata must be an object')
    if export:
        # Metadata indexes never replace or mutate the authoritative typed snapshot.
        project_ops = [op for op in export.get('dependencies', {}).get('operations', [])
                       if op.get('operation_type') == 'project']
        if len(project_ops) != 1 or project_ops[0].get('metadata', {}).get('fit_structure') != payload:
            raise ValueError('Export operation does not match typed project snapshot')
    resources = {name: base64.b64encode(content).decode('ascii') for name, content in entries.items()
                 if name not in {'project.json', 'mmfdb_export.json'}}
    return payload, export, resources


def _collisions(conn: Any, export: dict) -> dict[str, list[str]]:
    """Find actual persisted identity collisions for the preview and import gate."""
    collisions = {}
    for category, table, key in (
        ('operations', 'mmfdb_operation', 'operation_id'),
        ('artifacts', 'mmfdb_artifact', 'artifact_id'),
        ('objects', 'mmfdb_object', 'object_uuid'),
        ('parameters', 'mmfdb_parameter', 'parameter_uuid'),
    ):
        collisions[category] = [row[key] for row in export.get('dependencies', {}).get(category, [])
                                if row.get(key) and conn.execute(
                                    f'SELECT 1 FROM {table} WHERE {key} = ?', (row[key],),
                                ).fetchone()]
    return collisions


def import_preview_handler(*, db_path: str = '', auth: dict[str, Any] | None = None,
                           archive_base64: str | None = None, file_path: str | None = None,
                           archive_object_uuid: str | None = None) -> dict[str, Any]:
    """Preview a supplied portable content bundle against the authenticated database."""
    try:
        principal, conn, db = _auth_db(auth, db_path)
        data = None
        if archive_object_uuid:
            require_access(conn, principal, 'object', archive_object_uuid, PERM_READ)
            data = db.get_object(archive_object_uuid)
        payload, export, resources = _import_bundle(archive_base64, file_path, data)
        collisions = _collisions(conn, export)
        return {'ok': True, 'archive_kind': 'mmfdb_export' if export else 'chisurf_project',
                'has_collisions': any(collisions.values()), 'collisions': collisions,
                'origin': export.get('origin', {'project_name': payload.get('meta', {}).get('name', '')}),
                'entity_counts': {key: len(export.get('dependencies', {}).get(key, []))
                                  for key in ('operations', 'artifacts', 'objects')}}
    except Exception as exc:  # noqa: BLE001 - RPC boundary returns explicit failure
        return service_error(str(exc), error_code=OPERATION_FAILED, exception=exc)
    finally:
        if 'db' in locals():
            db.close()


def import_csp_handler(*, db_path: str = '', auth: dict[str, Any] | None = None,
                       archive_base64: str | None = None, file_path: str | None = None,
                       resolve_collisions: bool = False, archive_object_uuid: str | None = None) -> dict[str, Any]:
    """Publish supplied exact science through the immutable-version allocator."""
    try:
        principal, conn, db = _auth_db(auth, db_path)
        data = None
        if archive_object_uuid:
            require_access(conn, principal, 'object', archive_object_uuid, PERM_READ)
            data = db.get_object(archive_object_uuid)
        payload, export, resources = _import_bundle(archive_base64, file_path, data)
        collisions = _collisions(conn, export)
        if any(collisions.values()) and not resolve_collisions:
            raise ValueError('Collisions detected; set resolve_collisions=true')
        objects = {obj['object_uuid']: obj for obj in export.get('dependencies', {}).get('objects', [])}
        for artifact in export.get('dependencies', {}).get('artifacts', []):
            if artifact.get('artifact_kind') == 'raw_measurement':
                obj = objects[artifact['object_uuid']]
                resources[artifact['file_path']] = obj['data_base64']
        _decode_resources(resources)
        origin = export.get('origin', {})
        project_id = origin.get('project_id')
        existing = conn.execute(
            "SELECT operator_user_id FROM mmfdb_operation WHERE operation_type = 'project' "
            "AND json_extract(metadata_json, '$.project_id') = ?", (project_id,),
        ).fetchall() if project_id else []
        if existing and any(row[0] != principal.user_id for row in existing):
            project_id = None
        result = save_project_handler(db_path=db_path, auth=auth,
                                      project_name=payload.get('meta', {}).get('name', ''),
                                      project_payload=payload, project_id=project_id,
                                      notes='Imported from .cs.pto', resource_bundle=resources)
        if result.get('ok') is not True:
            return result
        remap = {'operations': {origin['version_id']: result['version_id']}} if origin.get('version_id') else {}
        return {**result, 'archive_kind': 'mmfdb_export' if export else 'chisurf_project',
                'collisions_resolved': collisions, 'id_remap': remap}
    except Exception as exc:  # noqa: BLE001 - RPC boundary returns explicit failure
        return service_error(str(exc), error_code=OPERATION_FAILED, exception=exc)
    finally:
        if 'db' in locals():
            db.close()


def register_services(dispatcher: Any, *, db_path: str = '') -> None:
    """Expose the same content-only project API on standalone and embedded servers."""
    for name, handler in {
        'save': save_project_handler, 'restore': restore_project_handler,
        'list': list_projects_handler, 'export_csp': export_csp_handler,
        'import_preview': import_preview_handler, 'import_csp': import_csp_handler,
        'delete_version': delete_version_handler, 'create_branch': create_branch_handler,
        'list_branches': list_branches_handler, 'version_graph': get_version_graph_handler,
        'artifacts': list_project_artifacts_handler, 'parameters': list_project_parameters_handler,
    }.items():
        dispatcher.register('project_browser.' + name,
                            lambda params, _handler=handler: _handler(db_path=db_path, **params))


def _resource_labels(value: Any) -> set[str]:
    """Find declared resource labels without opening their client-local paths."""
    labels = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"filename", "file_path", "source_file"} and isinstance(item, str) and item:
                labels.add(item)
            elif isinstance(item, (dict, list)):
                labels.update(_resource_labels(item))
    elif isinstance(value, list):
        for item in value:
            labels.update(_resource_labels(item))
    return labels
