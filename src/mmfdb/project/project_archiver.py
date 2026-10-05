"""Project archiver for MMFDB — decomposes projects into artifacts, parameters, and edges.

This module provides :func:`archive_project_to_mmfdb` which stores a ChiSurf
project with full provenance: source files in the object store, canonical datasets
and fit members as artifacts, scalar parameters and UID dependency edges. The
exact validated snapshot remains the sole scientific restore authority.
"""

from __future__ import annotations

import logging
import os
import uuid
from typing import Any

from mmfdb.models import (
    RELATIONSHIP_TYPES,
    validate_vocabulary,
)
from mmfdb.repository import MFDatabase
from mmfdb.samples.sample_manager import link_artifact_to_sample
from mmfdb.schema._sqlutil import _json_dumps, _json_loads

logger = logging.getLogger(__name__)


def _dataset_sample_id(ds_payload: dict[str, Any]) -> str:
    """Return the sample ID stored in a dataset payload, if any.

    Parameters
    ----------
    ds_payload : dict
        Serialized dataset payload.

    Returns
    -------
    str
        Sample ID, or an empty string.

    """
    metadata = ds_payload.get("metadata") or {}
    if isinstance(metadata, dict):
        return str(metadata.get("sample_id") or "")
    return ""


def archive_project_to_mmfdb(
    db: MFDatabase,
    project_payload: dict[str, Any],
    version_id: str,
    project_id: str,
    version_number: int,
    parent_version_id: str | None = None,
    branch_uuid: str | None = None,
    user_id: str = "user_default",
    notes: str = "",
    project_name: str = "",
    resource_bundle: dict[str, bytes] | None = None,
) -> dict[str, Any]:
    """Archive a ChiSurf project to MMFDB with full artifact decomposition.

    Within a single transaction this function:

    1. Creates a ``project`` operation record.
    2. For every dataset stores the source file (when available) in the
       content-addressed object store and registers a ``raw_measurement``
       artifact, then stores the derived data (arrays) as a
       ``processed_data`` artifact.
    3. Indexes canonical fit members, aggregate parameters and cross-model
       dependency edges without reconstructing or converting model state.
    4. Creates ``project_contains`` edges linking the project operation to
       every output artifact.
    5. Creates ``derived_from`` edges linking each dataset back to its
       source file.
    6. If *parent_version_id* is given, creates a ``supersedes`` edge
       forming a DAG across versions.
    7. Updates the branch head when *branch_uuid* is provided.

    Parameters
    ----------
    db : MFDatabase
        Active database connection.
    project_payload : dict
        Full project state (output of ``get_project_payload()``).
    version_id : str
        Unique identifier for this version (``ver_…``).
    project_id : str
        Stable project identifier (``proj_…``).
    version_number : int
        Version number within the branch scope.
    parent_version_id : str or None, optional
        Version ID this one supersedes.
    branch_uuid : str or None, optional
        Branch to record this version on.
    user_id : str, default='user_default'
        Owner user id.
    notes : str, default=''
        Free-text notes.
    project_name : str, default=''
        Display name when the snapshot metadata has no name.
    resource_bundle : dict of str to bytes or None, optional
        Client-supplied resource content keyed by original filename. A supplied
        bundle exclusively determines attachments and never authorizes reading
        server filesystem paths. None permits local direct-call source reads.

    Returns
    -------
    dict
        Summary with keys: ``operation_id``, ``dataset_artifacts``,
        ``fit_artifacts``, ``chinet_artifacts``, ``parameter_count``,
        ``edge_count``, ``object_count``.

    """
    validate_vocabulary("project", [
        "measurement_import", "validation", "burst_selection",
        "filtering", "fcs_correlation", "microtime_histogram",
        "tcspc_fitting", "model_fitting", "ndxplorer_selection",
        "ndxplorer_clustering", "project_snapshot",
        "project_restore", "archive_export",
        "tcspc_histogram_computation", "pda_histogram_computation",
        "pch_histogram_computation", "fcs_correlation_load",
        "tcspc_curve_load",
        "import", "burst_filtering", "gmm_fitting", "analysis",
        "fitting", "project_archive", "local_fit", "global_fit",
        "project", "analysis_run",
    ], "operation_type")

    meta = project_payload.get("meta") or {}
    project_name = meta.get("name", "") or project_payload.get("name", "") or project_name
    datasets = project_payload.get("datasets") or {}
    fits = project_payload.get("fits") or []
    project_format_version = project_payload.get("project_format_version", 4)
    chisurf_version = meta.get("chisurf_version", "")
    description = meta.get("description", "")
    created = meta.get("created", "")

    dataset_artifacts: list[str] = []
    fit_artifacts: list[str] = []
    chinet_artifacts: list[str] = []
    object_count = 0

    with db.transaction():
        # -- 1. Project operation -------------------------------------------
        db.record_operation(
            operation_id=version_id,
            operation_type="project",
            operator_user_id=user_id,
            status="succeeded",
            acl_owner_user_id=user_id,
            metadata={
                "project_id": project_id,
                "version_number": version_number,
                "branch_uuid": branch_uuid,
                "parent_version_id": parent_version_id,
                "project_name": project_name,
                "model_name": project_name,
                "notes": notes,
                "fit_count": len(fits),
                "dataset_count": len(datasets),
                "chisurf_version": chisurf_version,
                "project_format_version": project_format_version,
                "description": description,
                "created": created,
                # This is the authoritative restore record.  Dataset/fit/node
                # artifacts support provenance and queries, but their partial
                # reconstruction cannot faithfully recover global fit
                # structure, linked parameters, windows, or arbitrary UI
                # state.  The input has already crossed ChiSurf's JSON-safe
                # project boundary before it reaches this archiver.
                "fit_structure": project_payload,
                "ui_state": project_payload.get("ui", {}),
                "experiments": project_payload.get("experiments", {}),
            },
        )

        # -- 2. Per-dataset: source file + derived data --------------------
        ds_id_map: dict[str, str] = {}  # ds_id -> artifact_id
        for ds_idx, (ds_id, ds_payload) in enumerate(datasets.items()):
            if not isinstance(ds_payload, dict):
                continue
            filename = ds_payload.get("filename", "")
            source_object_uuid: str | None = None
            source_artifact_id: str | None = None

            # 2a. Source file → object store
            source_bytes = resource_bundle.get(filename) if resource_bundle is not None else None
            local_source = resource_bundle is None and filename and os.path.isfile(filename)
            if source_bytes is not None or local_source:
                source_ref = db.put_object(
                    data=source_bytes,
                    path=filename if local_source else None,
                    filename=str(filename),
                )
                source_object_uuid = source_ref["object_uuid"]
                source_artifact_id = "src_" + str(uuid.uuid5(
                    uuid.NAMESPACE_URL, _json_dumps([version_id, filename])
                ))
                db.register_artifact(
                    artifact_id=source_artifact_id,
                    artifact_kind="raw_measurement",
                    storage_mode="local_file",
                    file_path=str(filename),
                    object_uuid=source_object_uuid,
                    size_bytes=source_ref.get("size_bytes"),
                    validation_status="unvalidated",
                )
                db.record_operation_link(
                    operation_id=version_id,
                    artifact_id=source_artifact_id,
                    direction="input",
                    role="source_file",
                    ordinal=ds_idx,
                )
                sample_id = _dataset_sample_id(ds_payload)
                if sample_id:
                    link_artifact_to_sample(db, source_artifact_id, sample_id)
                object_count += 1

            # 2b. Derived data → object store
            reader_info = ds_payload.get("reader") or {}
            # The validated canonical dataset is the complete provenance index:
            # preserve dtype, mask, reader state and metadata without conversion.
            derived_payload = ds_payload
            derived_bytes = _json_dumps(derived_payload).encode("utf-8")
            derived_ref = db.put_object(
                data=derived_bytes,
                filename=f"{ds_id}.json",
                mime_type="application/json",
            )
            derived_object_uuid = derived_ref["object_uuid"]
            dataset_artifact_id = f"dataset:{version_id}:{ds_id}"
            db.register_artifact(
                artifact_id=dataset_artifact_id,
                artifact_kind="processed_data",
                storage_mode="embedded_json",
                object_uuid=derived_object_uuid,
                data_format="json",
                size_bytes=len(derived_bytes),
                metadata={
                    "ds_id": ds_id,
                    "name": ds_payload.get("name", ""),
                    "filename": filename,
                    "experiment_name": (ds_payload.get("experiment") or {}).get("name", ""),
                    "data_reader_module": reader_info.get("module", ""),
                    "data_reader_class": reader_info.get("class", ""),
                },
            )
            sample_id = _dataset_sample_id(ds_payload)
            if sample_id:
                link_artifact_to_sample(db, dataset_artifact_id, sample_id)
            db.record_operation_link(
                operation_id=version_id,
                artifact_id=dataset_artifact_id,
                direction="output",
                role="dataset",
                ordinal=ds_idx,
                metadata={"ds_id": ds_id},
            )
            ds_id_map[ds_id] = dataset_artifact_id
            dataset_artifacts.append(dataset_artifact_id)
            object_count += 1

            # 2c. project_contains edge
            db.add_edge(
                source_node_type="operation",
                source_node_id=version_id,
                target_node_type="artifact",
                target_node_id=dataset_artifact_id,
                relationship_type="project_contains",
                operation_id=version_id,
                metadata={"ds_id": ds_id},
            )

            # 2d. derived_from edge
            if source_artifact_id:
                db.add_edge(
                    source_node_type="artifact",
                    source_node_id=dataset_artifact_id,
                    target_node_type="artifact",
                    target_node_id=source_artifact_id,
                    relationship_type="derived_from",
                    operation_id=version_id,
                    metadata={"source_object_uuid": source_object_uuid},
                )

        # Resources referenced by readers/models need not be dataset filenames.
        # Preserve every supplied attachment, using labels only as metadata.
        dataset_filenames = {dataset.get("filename") for dataset in datasets.values()}
        for filename, content in (resource_bundle or {}).items():
            if filename in dataset_filenames:
                continue
            source_ref = db.put_object(data=content, filename=filename)
            source_artifact_id = "src_" + str(uuid.uuid5(
                uuid.NAMESPACE_URL, _json_dumps([version_id, filename])
            ))
            db.register_artifact(
                artifact_id=source_artifact_id,
                artifact_kind="raw_measurement",
                storage_mode="local_file",
                file_path=filename,
                object_uuid=source_ref["object_uuid"],
                size_bytes=source_ref.get("size_bytes"),
                validation_status="unvalidated",
            )
            db.record_operation_link(
                operation_id=version_id, artifact_id=source_artifact_id,
                direction="input", role="source_resource",
            )
            object_count += 1

        # -- 3. Index the validated canonical members and model parameters --
        parameter_ids: dict[tuple[str, str, str], str] = {}
        pending_links: list[tuple[str, dict[str, Any]]] = []
        for fit_idx, fit_record in enumerate(fits):
            fit_uid = fit_record["uid"]
            model_owners: list[tuple[str, str, dict[str, Any]]] = []
            for member_idx, member in enumerate(fit_record["members"]):
                member_uid = member["uid"]
                model = member["model"]
                fit_op_id = f"fit_{version_id}:{fit_uid}:{member_uid}"
                db.record_operation(
                    operation_id=fit_op_id,
                    operation_type="local_fit",
                    operator_user_id=user_id,
                    status="succeeded",
                    acl_owner_user_id=user_id,
                    metadata={
                        "fit_uid": fit_uid, "member_uid": member_uid,
                        "project_id": project_id, "version_id": version_id,
                    },
                )
                fit_artifact_id = f"fit_result:{version_id}:{fit_uid}:{member_uid}"
                db.register_artifact(
                    artifact_id=fit_artifact_id,
                    artifact_kind="fit_result",
                    storage_mode="embedded_json",
                    data_format="json",
                    data_json=_json_dumps({"fit": fit_record, "member_uid": member_uid}),
                    metadata={
                        "schema_name": "chisurf.project.v5",
                        "fit_uid": fit_uid, "member_uid": member_uid,
                        "fit_name": fit_record.get("name", ""),
                        "fit_index": fit_idx, "member_index": member_idx,
                        "model_module": model["model_module"],
                        "model_class": model["model_class"],
                    },
                )
                db.record_operation_link(
                    operation_id=fit_op_id, artifact_id=fit_artifact_id,
                    direction="output", role="fit_state",
                )
                db.add_edge(
                    source_node_type="operation", source_node_id=version_id,
                    target_node_type="artifact", target_node_id=fit_artifact_id,
                    relationship_type="project_contains", operation_id=version_id,
                )
                fit_artifacts.append(fit_artifact_id)
                for role, dataset_uid in {
                    "input_data": member["dataset_uid"],
                    **member.get("dependencies", {}),
                }.items():
                    db.record_operation_link(
                        operation_id=fit_op_id, artifact_id=ds_id_map[dataset_uid],
                        direction="input", role=role,
                    )
                model_owners.append((member_uid, fit_op_id, model))
            if fit_record.get("kind") == "group":
                model_owners.append((fit_uid, version_id, fit_record["aggregate_model"]))
            for owner_uid, operation_id, model in model_owners:
                for parameter in model["parameters"]:
                    uid = parameter["uid"]
                    # The parameter table upserts its primary key. Scope that
                    # index key to a version, retaining the science UID verbatim.
                    key = (fit_uid, owner_uid, uid)
                    parameter_id = str(uuid.uuid5(uuid.NAMESPACE_URL, _json_dumps([version_id, *key])))
                    parameter_ids[key] = parameter_id
                    link = parameter.get("link_target")
                    parameter_type = "linked" if link else "fixed" if parameter["fixed"] else "free"
                    bounds = parameter["bounds"]
                    db.record_parameter(
                        parameter_uuid=parameter_id, operation_id=operation_id,
                        name=parameter["name"], value=parameter["value"],
                        initial_value=parameter["value"], standard_error=parameter.get("error_estimate"),
                        lower_bound=bounds[0], upper_bound=bounds[1],
                        bounds_on=parameter["bounds_on"], parameter_type=parameter_type,
                        metadata={
                            "schema_name": "chisurf.project.v5",
                            "fit_uid": fit_uid, "member_uid": owner_uid,
                            "fit_parameter_uid": uid, "parameter": parameter,
                            "link_target": link,
                        },
                    )
                    if link:
                        pending_links.append((parameter_id, parameter))
        # All members and global parameters exist before cross-model links.
        for parameter_id, parameter in pending_links:
            link = parameter["link_target"]
            target_key = (link["fit_uid"], link["member_uid"], link["parameter_uid"])
            db.add_edge(
                source_node_type="parameter", source_node_id=parameter_id,
                target_node_type="parameter", target_node_id=parameter_ids[target_key],
                relationship_type="parameter_depends_on", operation_id=version_id,
                metadata={"parameter_uid": parameter["uid"], "link_target": link},
            )

        # -- 4. Version lineage edge ----------------------------------------
        if parent_version_id:
            validate_vocabulary("supersedes", RELATIONSHIP_TYPES, "relationship_type")
            db.add_edge(
                source_node_type="operation",
                source_node_id=version_id,
                target_node_type="operation",
                target_node_id=parent_version_id,
                relationship_type="supersedes",
                operation_id=version_id,
                metadata={
                    "branch_uuid": branch_uuid,
                    "version_number": version_number,
                },
            )

        # -- 5. Update branch head ------------------------------------------
        if branch_uuid:
            try:
                db.update_branch_head(branch_uuid, version_id)
            except Exception:
                logger.warning(
                    "Failed to update branch head for %s",
                    branch_uuid,
                    exc_info=True,
                )

        # Project history is part of the archive, not a best-effort projection.
        # Keep it in the same outer transaction so state and history cannot
        # diverge after a partial failure.
        history_events = (
            (project_payload.get("extra") or {}).get("history_events") or []
        )
        if history_events:
            from mmfdb.lifecycle import event_log

            event_ids = tuple(
                dict.fromkeys(
                    str(event["event_id"])
                    for event in history_events
                    if event.get("event_id")
                )
            )
            for event in history_events:
                if not event_log.append_event(event, db=db, strict=True):
                    raise RuntimeError("Project history event has no event_id")
            scoped = event_log.scope_events(
                event_ids,
                project_id=project_id,
                operation_id=version_id,
                db=db,
                strict=True,
            )
            if scoped != len(event_ids):
                raise RuntimeError("Project history scoping was incomplete")

    # Count actual parameters and edges created for this version
    escaped = version_id.replace("_", "\\_")
    param_count = db.conn.execute(
        "SELECT COUNT(*) FROM mmfdb_parameter WHERE operation_id = ? OR operation_id LIKE ? ESCAPE '\\'",
        (version_id, f"fit\\_{escaped}:%"),
    ).fetchone()[0]
    edge_count = db.conn.execute(
        "SELECT COUNT(*) FROM mmfdb_edge WHERE operation_id = ?",
        (version_id,),
    ).fetchone()[0]

    return {
        "operation_id": version_id,
        "dataset_artifacts": dataset_artifacts,
        "fit_artifacts": fit_artifacts,
        "chinet_artifacts": chinet_artifacts,
        "parameter_count": param_count,
        "edge_count": edge_count,
        "object_count": object_count,
    }


def _parse_ds_id(aid: str) -> str | None:
    if aid.startswith("dataset:"):
        parts = aid.split(":", 2)
        if len(parts) == 3 and parts[2]:
            return parts[2]
    return None


def _regroup_fit_artifacts(
    entries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Rebuild fit-group records from per-local-fit ``fit_result`` artifacts.

    A global fit is archived as one artifact per local fit, every one carrying
    the same fit UID in its envelope. Restoring them one-to-one would yield N
    fit groups that each hold a single local fit and all share an ``id`` --
    global fits silently shattered, and duplicate UIDs downstream. This merges
    them back into one record per UID.

    Order is taken from the ``fit_index`` / ``local_fit_index`` recorded at
    archive time; archives written before those existed fall back to the order
    the artifacts were encountered, since ``lf_id`` is not necessarily the
    local-fit index and artifact-id ordering is therefore unreliable.

    Parameters
    ----------
    entries : list of dict
        One entry per ``fit_result`` artifact, with keys ``artifact_id``,
        ``envelope``, ``local_fit``, ``fit_index`` and ``local_fit_index``.

    Returns
    -------
    list of dict
        Fit-group records with ``id``, ``name``, ``model_name``, ``fit_range``,
        ``plot_state`` and a ``local_fits`` list.
    """
    grouped: dict[str, dict[str, Any]] = {}
    seen_artifacts: set[str] = set()

    for order, entry in enumerate(entries):
        artifact_id = entry.get("artifact_id") or ""
        # The same artifact can be reached both as a project output and through
        # the fit-operation query; counting it twice would duplicate local fits.
        if artifact_id and artifact_id in seen_artifacts:
            continue
        if artifact_id:
            seen_artifacts.add(artifact_id)

        envelope = entry.get("envelope") or {}
        # Fits archived without a UID cannot be merged with anything, so give
        # each its own bucket rather than collapsing them all into one group.
        uid = envelope.get("id") or f"__anonymous__{order}"
        group = grouped.get(uid)
        if group is None:
            group = {**envelope, "local_fits": [], "_order": order, "_locals": []}
            grouped[uid] = group

        fit_index = entry.get("fit_index")
        if isinstance(fit_index, int):
            group["_order"] = min(group["_order"], fit_index)
        group["_locals"].append((entry.get("local_fit_index"), order, entry.get("local_fit")))

    records: list[dict[str, Any]] = []
    for group in sorted(grouped.values(), key=lambda g: g["_order"]):
        locals_ = group.pop("_locals")
        if all(isinstance(lf_idx, int) for lf_idx, _, _ in locals_):
            locals_.sort(key=lambda item: item[0])
        else:
            locals_.sort(key=lambda item: item[1])
        group["local_fits"] = [lf for _, _, lf in locals_ if lf]
        group.pop("_order", None)
        records.append(group)
    return records


def restore_project_from_artifacts(
    db: MFDatabase,
    version_id: str,
) -> dict[str, Any] | None:
    """Restore a project from its MMFDB artifacts.

    Queries all output artifacts for the given version and reconstructs
    the project payload.  Returns ``None`` if no artifacts are found
    (indicating a legacy archive that should be restored via the JSON
    blob instead).

    Parameters
    ----------
    db : MFDatabase
        Active database connection.
    version_id : str
        Version identifier (``ver_…``).

    Returns
    -------
    dict or None
        Reconstructed project payload with ``datasets``, ``fits``, and
        ``chinet_sessions`` keys, or ``None`` if no artifacts exist.

    """
    # Query for artifacts linked to the project operation
    artifacts = db.get_operation_artifacts(version_id, direction="output")
    if not artifacts:
        return None

    datasets: dict[str, Any] = {}
    fits: list[dict[str, Any]] = []
    #: One entry per ``fit_result`` artifact, regrouped into fit records below.
    fit_groups: list[dict[str, Any]] = []
    chinet_sessions: list[dict[str, Any]] = []
    fit_operation_ids: set[str] = set()
    project_metadata: dict[str, Any] = {}

    # Query for artifacts linked to fit operations scoped to this version
    # Use LIKE to match: fit_{version_id}:* with parameterized query and ESCAPE
    escaped_vid = version_id.replace("_", "\\_")
    fit_artifacts = db.conn.execute(
        """SELECT mmfdb_artifact.*, mmfdb_operation_artifact.role,
                 mmfdb_operation_artifact.direction, mmfdb_operation.operation_id
            FROM mmfdb_operation_artifact
            JOIN mmfdb_artifact ON mmfdb_artifact.artifact_id = mmfdb_operation_artifact.artifact_id
            JOIN mmfdb_operation ON mmfdb_operation.operation_id = mmfdb_operation_artifact.operation_id
            WHERE mmfdb_operation.operation_type = 'local_fit'
              AND mmfdb_operation.operation_id LIKE ? ESCAPE '\\'
              AND mmfdb_operation_artifact.deleted_at IS NULL
              AND mmfdb_artifact.deleted_at IS NULL
              AND mmfdb_operation_artifact.direction = 'output'""",
        (f"fit\\_{escaped_vid}:%",),
    ).fetchall()
    artifacts.extend(fit_artifacts)

    for art in artifacts:
        art = dict(art) if not isinstance(art, dict) else art
        kind = art.get("artifact_kind", "")
        data_json = art.get("data_json")

        if data_json and isinstance(data_json, str):
            data = _json_loads(data_json)
        elif art.get("object_uuid"):
            try:
                blob = db.get_object(art["object_uuid"])
                data = _json_loads(blob.decode("utf-8"))
            except Exception:
                logger.warning(
                    "Failed to read object %s from store",
                    art.get("object_uuid"),
                    exc_info=True,
                )
                data = None
        else:
            data = None

        if data is None:
            continue

        role = art.get("role", "")

        if kind == "processed_data" and role == "dataset":
            meta = art.get("metadata_json") or {}
            if isinstance(meta, str):
                meta = _json_loads(meta) or {}
            ds_id = (
                meta.get("ds_id")
                or meta.get("dataset_uid")
                or _parse_ds_id(art.get("artifact_id", ""))
                or art.get("artifact_id")
                or str(uuid.uuid4())
            )
            datasets[ds_id] = data

        elif kind == "fit_result":
            meta = art.get("metadata_json") or {}
            if isinstance(meta, str):
                meta = _json_loads(meta) or {}
            fit_data_is_full_record = isinstance(data, dict) and "local_fit" in data
            if fit_data_is_full_record:
                envelope = {
                    "id": data.get("id", ""),
                    "name": data.get("name", ""),
                    "model_name": data.get("model_name", ""),
                    "fit_range": data.get("fit_range"),
                    "plot_state": data.get("plot_state"),
                }
                local_fit = data.get("local_fit", {})
            else:
                envelope = {
                    "id": meta.get("fit_id", str(uuid.uuid4())),
                    "name": meta.get("fit_name", "Restored Fit"),
                    "model_name": meta.get("model_name", ""),
                    "fit_range": meta.get("fit_range"),
                    "plot_state": meta.get("plot_state"),
                }
                local_fit = data
            # A global fit is archived as one artifact per local fit, all sharing
            # the fit UID. Collect them under that UID so the group is rebuilt
            # whole; appending per artifact would return N single-local-fit
            # groups that all carry the *same* id, shattering global fits and
            # colliding on anything keyed by fit id.
            fit_groups.append({
                "artifact_id": art.get("artifact_id", ""),
                "envelope": envelope,
                "local_fit": local_fit,
                "fit_index": meta.get("fit_index"),
                "local_fit_index": meta.get("local_fit_index"),
            })
            fit_op_id = art.get("operation_id", "")
            if fit_op_id:
                fit_operation_ids.add(fit_op_id)

        elif kind == "chinet_session":
            chinet_sessions.append(data)

    fits = _regroup_fit_artifacts(fit_groups)

    if not datasets and not fits:
        return None

    # Query the project operation metadata for ui_state and experiments
    project_op = db.conn.execute(
        "SELECT metadata_json FROM mmfdb_operation WHERE operation_id = ?",
        (version_id,),
    ).fetchone()
    if project_op:
        project_op = dict(project_op)
        op_meta = _json_loads(project_op.get("metadata_json")) or {}
        project_metadata = op_meta

    # Query parameters for each fit operation
    all_parameters: dict[str, list[dict[str, Any]]] = {}
    for fit_op_id in fit_operation_ids:
        rows = db.conn.execute(
            "SELECT * FROM mmfdb_parameter WHERE operation_id = ?",
            (fit_op_id,),
        ).fetchall()
        all_parameters[fit_op_id] = [dict(r) for r in rows]

    # Query dependency edges scoped to this version's operations
    dependency_edges: list[dict[str, Any]] = []
    if fit_operation_ids:
        placeholders = ",".join("?" for _ in fit_operation_ids)
        rows = db.conn.execute(
            f"SELECT * FROM mmfdb_edge WHERE relationship_type = 'parameter_depends_on' "
            f"AND operation_id IN ({placeholders})",
            list(fit_operation_ids),
        ).fetchall()
        dependency_edges = [dict(r) for r in rows]

    # Operation-history projection: read the durable event log for this project
    # and hand it back under the same ``extra.history_events`` seam the .csp path
    # uses, so the consumer rehydrates cs.history identically (PRD-43).
    history_events: list[dict[str, Any]] = []
    restore_project_id = project_metadata.get("project_id")
    if restore_project_id:
        from mmfdb.lifecycle import event_log
        history_events = event_log.read_events(project_id=restore_project_id, db=db)

    return {
        "datasets": datasets,
        "fits": fits,
        "chinet_sessions": chinet_sessions,
        "parameters": all_parameters,
        "dependency_edges": dependency_edges,
        "ui_state": project_metadata.get("ui_state", {}),
        "experiments": project_metadata.get("experiments", {}),
        "extra": {"history_events": history_events},
    }
