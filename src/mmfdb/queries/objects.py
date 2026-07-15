"""Object-store queries.

Content-addressed blob storage access (put/get/list/delete) and artifact-file
materialization — extracted from the MFDatabase god-class (PRD-26). Routes
through the object store; methods resolve cross-concern calls via the MFDatabase
MRO.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

from mmfdb.schema._sqlutil import _json_dumps


class ObjectStoreMixin:
    """Repository operations backed by a configured content-addressed store."""

    def _get_object_store(self):
        """Return the configured shared object-store backend."""
        if not hasattr(self, "_object_store") or self._object_store is None:
            from mmfdb.store.object_store import create_object_store

            self._object_store = create_object_store()
        return self._object_store

    def put_object(
        self,
        path: str | os.PathLike | None = None,
        data: bytes | None = None,
        filename: str | None = None,
        mime_type: str | None = None,
        metadata: dict[str, Any] | None = None,
        created_by_user_uuid: str | None = None,
        owner_user_id: str | None = None,
    ) -> dict[str, Any]:
        """Store a file or bytes in the object store and register in mmfdb_object.

        Parameters
        ----------
        path : str or PathLike, optional
            Path to the file to store. Mutually exclusive with ``data``.
        data : bytes, optional
            Binary content to store. Mutually exclusive with ``path``.
        filename : str, optional
            Original filename to record in metadata.
        mime_type : str, optional
            MIME type of the content.
        metadata : dict, optional
            Additional metadata to store as JSON.
        created_by_user_uuid : str, optional
            UUID of the user who created the object.
        owner_user_id : str, optional
            Authenticated owner of this logical reference to the deduplicated blob.

        Returns
        -------
        dict
            Object reference with keys: ``object_uuid``, ``content_md5``,
            ``size_bytes``, ``original_filename``, ``deduplicated``, ``storage_path``.
        """
        store = self._get_object_store()
        if path is not None and data is not None:
            raise ValueError("Cannot specify both path and data")
        if path is not None:
            ref = store.put_from_path(Path(path), original_filename=filename)
        elif data is not None:
            ref = store.put_bytes(data, filename=filename or "unnamed")
        else:
            raise ValueError("Must specify either path or data")

        try:
            with self._transaction():
                # One SQL statement owns the content-address conflict.  A prior
                # read followed by INSERT races when two processes publish the same
                # content concurrently.
                row = self.conn.execute(
                    """INSERT INTO mmfdb_object (
                           object_uuid, content_md5, content_sha256, original_filename, size_bytes,
                           mime_type, storage_path, refcount, metadata_json,
                           created_by_user_uuid
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                       ON CONFLICT(content_md5) DO UPDATE SET
                           refcount = mmfdb_object.refcount + 1
                       RETURNING object_uuid, refcount""",
                    (
                        ref.uuid,
                        ref.md5,
                        ref.sha256,
                        ref.original_filename,
                        ref.size,
                        mime_type,
                        ref.storage_path,
                        _json_dumps(metadata),
                        created_by_user_uuid,
                    ),
                ).fetchone()
                object_uuid = row["object_uuid"]
                refcount = int(row["refcount"])
                deduplicated = object_uuid != ref.uuid or ref.deduplicated
                if owner_user_id:
                    self.conn.execute(
                        """INSERT INTO mmfdb_object_reference
                               (object_uuid, user_id, refcount, original_filename,
                                metadata_json, created_at, updated_at)
                           VALUES (?, ?, 1, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                           ON CONFLICT(object_uuid, user_id) DO UPDATE SET
                               refcount = mmfdb_object_reference.refcount + 1,
                               original_filename = excluded.original_filename,
                               metadata_json = excluded.metadata_json,
                               updated_at = CURRENT_TIMESTAMP""",
                        (
                            object_uuid,
                            owner_user_id,
                            filename,
                            _json_dumps(metadata),
                        ),
                    )
                self.add_audit_log(
                    action="create" if not deduplicated else "reference",
                    target_type="object",
                    target_id=object_uuid,
                    details={"content_md5": ref.md5, "deduplicated": deduplicated},
                )
        except Exception:
            # Filesystems cannot join SQLite transactions.  Rollback the row
            # first, then remove only a blob published by this call and still
            # unreferenced in the database. Existing deduplicated blobs belong
            # to earlier successful registrations and must never be removed.
            referenced = self.conn.execute(
                "SELECT 1 FROM mmfdb_object WHERE content_md5 = ? LIMIT 1",
                (ref.md5,),
            ).fetchone()
            if not ref.deduplicated and referenced is None:
                store.delete(ref.md5)
            raise
        if not ref.deduplicated and getattr(self, "_transaction_depth", 0) > 0:

            def remove_rolled_back_blob() -> None:
                referenced = self.conn.execute(
                    "SELECT 1 FROM mmfdb_object WHERE content_md5 = ? LIMIT 1",
                    (ref.md5,),
                ).fetchone()
                if referenced is None:
                    store.delete(ref.md5)

            self._on_rollback(remove_rolled_back_blob)
        return {
            "object_uuid": object_uuid,
            "content_md5": ref.md5,
            "content_sha256": ref.sha256,
            "size_bytes": ref.size,
            "original_filename": ref.original_filename,
            "deduplicated": deduplicated,
            "storage_path": ref.storage_path,
            "refcount": refcount,
        }

    def get_object(self, object_uuid: str) -> bytes:
        """Retrieve blob content by object UUID.

        Parameters
        ----------
        object_uuid : str
            The object UUID.

        Returns
        -------
        bytes
            The stored content.
        """
        row = self.dao.get("mmfdb_object", object_uuid, include_deleted=True)
        if row is None:
            raise KeyError(f"Object not found: {object_uuid}")
        store = self._get_object_store()
        return store.get(row["content_md5"], sha256=row.get("content_sha256"))

    def get_object_info(self, object_uuid: str) -> dict[str, Any] | None:
        """Retrieve object metadata by UUID.

        Parameters
        ----------
        object_uuid : str
            The object UUID.

        Returns
        -------
        dict or None
            Object metadata, or None if not found.
        """
        # PRD-26 Task 2: parameterised, schema-driven get-by-PK (was a hand SELECT).
        return self.dao.get("mmfdb_object", object_uuid, include_deleted=True)

    def get_object_path(self, object_uuid: str) -> Path:
        """Return the filesystem path for an object.

        Parameters
        ----------
        object_uuid : str
            The object UUID.

        Returns
        -------
        Path
            Path to the stored blob.
        """
        row = self.dao.get("mmfdb_object", object_uuid, include_deleted=True)
        if row is None:
            raise KeyError(f"Object not found: {object_uuid}")
        store = self._get_object_store()
        return store.get_path(row["content_md5"], sha256=row.get("content_sha256"))

    def materialize_artifact_file(self, artifact_id: str, *, into: str | None = None) -> str:
        """Copy an artifact's stored blob to a temp file and return its path.

        The object store is content-addressed, so its blob carries no file
        extension; readers such as ``tttrlib`` infer the container from the suffix.
        This copies the blob into a fresh temp file that carries the artifact's
        recorded ``data_format`` suffix so a re-read works — the materialization
        primitive behind replay/recompute (PRD-21 Task 2).

        Parameters
        ----------
        artifact_id : str
            Artifact whose stored object should be materialized.
        into : str, optional
            Directory for the temp file (defaults to the system temp dir).

        Returns
        -------
        str
            Path to the materialized copy.
        """
        artifact = self.get_artifact(artifact_id)
        if not artifact:
            raise KeyError(f"artifact {artifact_id!r} not found")
        object_uuid = artifact.get("object_uuid")
        if not object_uuid:
            raise ValueError(f"artifact {artifact_id!r} has no stored object to materialize")
        row = self.dao.get("mmfdb_object", object_uuid, include_deleted=True)
        if row is None:
            raise KeyError(f"Object not found: {object_uuid}")
        store = self._get_object_store()
        data_format = (artifact.get("data_format") or "").lstrip(".")
        suffix = f".{data_format}" if data_format else ""
        fd, tmp = tempfile.mkstemp(prefix="mmfdb_materialize_", suffix=suffix, dir=into)
        os.close(fd)
        Path(tmp).unlink()
        return str(
            store.materialize(
                row["content_md5"],
                Path(tmp),
                sha256=row.get("content_sha256"),
            )
        )

    def delete_object(
        self, object_uuid: str, *, owner_user_id: str | None = None
    ) -> dict[str, Any]:
        """Delete an object or decrement its refcount.

        Parameters
        ----------
        object_uuid : str
            The object UUID.
        owner_user_id : str, optional
            Owner whose logical reference should be decremented.

        Returns
        -------
        dict
            Result with keys: ``deleted`` (bool), ``refcount`` (int).
        """
        result: dict[str, Any]
        gc_result = {"deleted": False}
        with self._transaction():
            row = self.conn.execute(
                "SELECT content_md5, refcount FROM mmfdb_object WHERE object_uuid = ?",
                (object_uuid,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Object not found: {object_uuid}")
            live_artifact_count = int(
                self.conn.execute(
                    "SELECT COUNT(*) FROM mmfdb_artifact "
                    "WHERE object_uuid = ? AND deleted_at IS NULL",
                    (object_uuid,),
                ).fetchone()[0]
            )
            if live_artifact_count:
                suffix = "" if live_artifact_count == 1 else "s"
                raise ValueError(
                    f"Object {object_uuid!r} is referenced by {live_artifact_count} "
                    f"live artifact{suffix}; delete the artifact references first."
                )

            md5 = row["content_md5"]
            reference_exhausted = False
            if owner_user_id:
                reference = self.conn.execute(
                    "SELECT refcount FROM mmfdb_object_reference "
                    "WHERE object_uuid = ? AND user_id = ?",
                    (object_uuid, owner_user_id),
                ).fetchone()
                if reference is None:
                    raise ValueError("No object reference is owned by this user")
                if int(reference["refcount"]) > 1:
                    self.conn.execute(
                        "UPDATE mmfdb_object_reference SET refcount = refcount - 1, "
                        "updated_at = CURRENT_TIMESTAMP WHERE object_uuid = ? AND user_id = ?",
                        (object_uuid, owner_user_id),
                    )
                else:
                    self.conn.execute(
                        "DELETE FROM mmfdb_object_reference WHERE object_uuid = ? AND user_id = ?",
                        (object_uuid, owner_user_id),
                    )
                    reference_exhausted = True
            decremented = self.conn.execute(
                "UPDATE mmfdb_object SET refcount = refcount - 1 "
                "WHERE object_uuid = ? AND refcount > 1 RETURNING refcount",
                (object_uuid,),
            ).fetchone()
            if decremented is None:
                deleted = self.conn.execute(
                    "DELETE FROM mmfdb_object WHERE object_uuid = ? AND refcount <= 1 "
                    "RETURNING content_md5",
                    (object_uuid,),
                ).fetchone()
                if deleted is None:
                    raise RuntimeError("Object reference changed concurrently; retry deletion")
                self.add_audit_log(
                    action="delete",
                    target_type="object",
                    target_id=object_uuid,
                    details={"content_md5": md5, "physical_gc_pending": True},
                )
                store = self._get_object_store()

                def delete_committed_blob() -> None:
                    gc_result["deleted"] = store.delete(md5)

                self._on_commit(delete_committed_blob)
                result = {"deleted": True, "refcount": 0}
            else:
                new_refcount = int(decremented["refcount"])
                self.add_audit_log(
                    action="dereference",
                    target_type="object",
                    target_id=object_uuid,
                    details={"content_md5": md5, "new_refcount": new_refcount},
                )
                result = {"deleted": False, "refcount": new_refcount}

            if owner_user_id and reference_exhausted and not result["deleted"]:
                replacement = self.conn.execute(
                    "SELECT user_id FROM mmfdb_object_reference "
                    "WHERE object_uuid = ? ORDER BY created_at, user_id LIMIT 1",
                    (object_uuid,),
                ).fetchone()
                acl = self.conn.execute(
                    "SELECT owner_user_id FROM mmfdb_object_acl "
                    "WHERE object_type = 'object' AND object_id = ? AND deleted_at IS NULL",
                    (object_uuid,),
                ).fetchone()
                if acl and acl["owner_user_id"] == owner_user_id and replacement:
                    self.conn.execute(
                        "UPDATE mmfdb_object_acl SET owner_user_id = ?, updated_at = CURRENT_TIMESTAMP "
                        "WHERE object_type = 'object' AND object_id = ?",
                        (replacement["user_id"], object_uuid),
                    )
                self.conn.execute(
                    "DELETE FROM mmfdb_acl_entry WHERE object_type = 'object' "
                    "AND object_id = ? AND subject_type = 'user' AND subject_id = ?",
                    (object_uuid, owner_user_id),
                )

        if result["deleted"]:
            result["blob_deleted"] = gc_result["deleted"]
            result["blob_gc_scheduled"] = not gc_result["deleted"]
        return result

    def list_objects(
        self,
        filename: str | None = None,
        user_uuid: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """List objects with optional filtering.

        Parameters
        ----------
        filename : str, optional
            Filter by original filename (substring match).
        user_uuid : str, optional
            Filter by creator user UUID.
        limit : int
            Maximum number of results.
        offset : int
            Offset for pagination.

        Returns
        -------
        list of dict
            List of object records.
        """
        limit = max(1, min(int(limit), 1000))
        offset = max(0, int(offset))
        query = "SELECT * FROM mmfdb_object WHERE 1=1"
        params: list[Any] = []
        if filename is not None:
            query += " AND original_filename LIKE ?"
            params.append(f"%{filename}%")
        if user_uuid is not None:
            query += " AND created_by_user_uuid = ?"
            params.append(user_uuid)
        query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        return [dict(r) for r in self.conn.execute(query, params).fetchall()]
