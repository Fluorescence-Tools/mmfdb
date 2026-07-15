import hashlib
import json
import logging
import os
import platform
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

from mmfdb.models import (
    PARAMETER_TYPES,
    validate_vocabulary,
)
from mmfdb.queries.analysis import AnalysisMixin
from mmfdb.queries.artifacts import ArtifactOpsMixin
from mmfdb.queries.branches import BranchMixin
from mmfdb.queries.experiments import ExperimentMixin
from mmfdb.queries.lifecycle import LifecycleMixin
from mmfdb.queries.objects import ObjectStoreMixin
from mmfdb.queries.parameters import ParameterMixin
from mmfdb.queries.probes import ProbeMixin
from mmfdb.queries.protocols import ProtocolMixin
from mmfdb.queries.samples import SampleMixin
from mmfdb.queries.setups import SetupCalibMixin
from mmfdb.queries.studies import StudyMixin
from mmfdb.queries.users import UserDeviceMixin
from mmfdb.schema import schema
from mmfdb.schema._sqlutil import (
    _json_dumps,
    _json_loads,
    _row_to_dict,
    _utc_now,
)
from mmfdb.security.base import MMFDBClientBase
from mmfdb.store.database_resolver import (
    backup_database_before_migration,
    resolve_database_path,
    restore_database_backup,
)
from mmfdb.store.sql_backend import (
    DatabaseCapabilityError,
    DatabaseTarget,
    PostgreSQLConnection,
    connect_postgresql,
    connection_dialect,
    parse_database_target,
    postgres_schema_version,
    validate_postgresql_schema,
)
from mmfdb.store.transactions import transaction as _transaction

if TYPE_CHECKING:
    from mmfdb.provenance.lineage import Lineage
    from mmfdb.schema.dao import DictionaryDao

logger = logging.getLogger(__name__)

class MFDatabase(
    AnalysisMixin,
    ArtifactOpsMixin,
    BranchMixin,
    ExperimentMixin,
    LifecycleMixin,
    ObjectStoreMixin,
    ParameterMixin,
    ProbeMixin,
    ProtocolMixin,
    SampleMixin,
    SetupCalibMixin,
    StudyMixin,
    UserDeviceMixin,
    MMFDBClientBase,
):

    _VALID_ENUMS = {
        # ``category`` is the canonical optical-component class used by the
        # mmfdb-admin "Spectra" radio tabs. Fluorophores keep the finer
        # ``organic_dye``/``protein``/… distinction; optical components use the
        # coarse ``filter``/``dichroic``/``detector``/``light_source`` classes.
        "category": [
            "fluorophore", "organic_dye", "protein", "nanoparticle", "quantum_dot",
            "filter", "dichroic", "detector", "light_source", "other",
        ],
        "probe_origin": ["extrinsic", "intrinsic", "other"],
        "probe_link_type": ["covalent", "non-covalent", "other"],
        "fluorophore_type": ["unspecified", "small_molecule", "protein_domain"],
        "reactive_probe_flag": ["yes", "no"],
    }

    _PROP_ALIASES = {
        "abs_max": ["abs_max", "absorption maximum", "λabs", "excitation max", "ex_max", "abs_peak", "λex"],
        "em_max": ["em_max", "emission maximum", "λfl", "emission max", "em_max", "em_peak", "λem"],
        "qy": ["qy", "fluorescence quantum yield", "ηfl", "quantum yield", "phi_acceptor", "phi", "qy_d", "phi_d"],
        "lifetime": ["lifetime", "fluorescence lifetime", "τfl", "tau", "tau_d", "tau_0"],
        "ext_coeff": ["ext_coeff", "molar extinction coefficient", "εmax", "extinction coefficient", "epsilon", "molar_ec"],
        # Optical-component (filter / dichroic / detector) properties — kept under
        # canonical keys so every scraper surfaces them identically in the GUI.
        "cut_on": ["cut_on", "cut-on", "cut on", "cut-on wavelength (nm)", "cut-on wavelength", "cuton"],
        "cut_off": ["cut_off", "cut-off", "cut off", "cut-off wavelength (nm)", "cut-off wavelength", "cutoff"],
        "center_wavelength": [
            "center_wavelength", "center wavelength", "center wavelength (nm)",
            "central wavelength", "cwl", "wavelength (nm)",
        ],
        "bandwidth": ["bandwidth", "bandwidth (nm)", "fwhm", "fwhm (nm)", "notch bandwidth (nm)", "bandwidth fwhm (nm)"],
        "optical_density": ["optical_density", "optical density", "od"],
        # CAS Registry Number — chemical identity for dyes/compounds.
        "cas": ["cas", "cas number", "cas_number", "cas nbr", "cas no", "cas#",
                "cas registry number", "casrn"],
    }

    def __init__(
        self,
        db_path: str | os.PathLike | None = None,
        readonly: bool = False,
        connection: sqlite3.Connection | PostgreSQLConnection | None = None,
        enforce_foreign_keys: bool = True,
        *,
        owns_connection: bool | None = None,
    ):
        import os as _os
        self._os = _os
        if db_path is None and connection is None:
            db_path = resolve_database_path()
        self.database_target: DatabaseTarget | None = (
            parse_database_target(db_path) if db_path is not None else None
        )
        self.database_url: str | None = None
        if self.database_target is None:
            self.db_path = None
        elif self.database_target.is_sqlite:
            sqlite_location = self.database_target.location
            self.db_path = (
                sqlite_location
                if sqlite_location == ":memory:"
                else _os.path.abspath(sqlite_location)
            )
            self.database_target = parse_database_target(self.db_path)
        else:
            # Preserve the path/URL distinction throughout the repository.
            # Treating a URL as a path is how backup and relative-file code can
            # accidentally create local files named ``postgresql:``.
            self.db_path = None
            self.database_url = self.database_target.location
        self.readonly = readonly
        self.enforce_foreign_keys = enforce_foreign_keys
        self._conn: sqlite3.Connection | PostgreSQLConnection | None = None
        self._owns_connection = connection is None if owns_connection is None else owns_connection
        self.migration_report = None
        if connection is not None:
            dialect = connection_dialect(connection)
            if self.database_target is None:
                if dialect == "sqlite":
                    self.database_target = parse_database_target(":memory:")
                else:
                    raise ValueError(
                        "Borrowed PostgreSQL connections require their database URL "
                        "as db_path so schema capabilities are explicit"
                    )
            elif self.database_target.dialect != dialect:
                raise ValueError(
                    f"Connection dialect {dialect!r} does not match target "
                    f"{self.database_target.dialect!r}"
                )
            if connection.in_transaction:
                raise ValueError("Cannot attach MFDatabase to a connection with an active transaction")
            self._conn = connection
            self._configure_connection()
            self._initialize_schema()
        elif db_path is not None:
            self.connect()

    @property
    def conn(self) -> sqlite3.Connection | PostgreSQLConnection:
        if self._conn is None:
            raise RuntimeError("Database not connected. Call connect() first.")
        return self._conn

    @property
    def dao(self) -> "DictionaryDao":
        """Dictionary-/schema-driven CRUD accessor (PRD-26 Task 1).

        Provides parameterised, schema-whitelisted ``insert/get/list/update/
        soft_delete`` over the live tables, built lazily from the connection
        after the schema is reconciled. CRUD-shaped repository methods are being
        migrated onto this to remove hand-written/f-string SQL.
        """
        dao = getattr(self, "_dao", None)
        if dao is None:
            from mmfdb.schema.dao import DictionaryDao

            dao = DictionaryDao.from_connection(self.conn)
            self._dao = dao
        return dao

    @property
    def lineage(self) -> "Lineage":
        """Lineage query API over the operation graph (PRD-21 Task 1).

        Artifact-centric ancestry/descent and the provenance-graph projection
        (``ancestors``/``descendants``/``what_used``/``provenance_graph``), built
        lazily from the connection. Call sites should use this instead of rolling
        their own ``mmfdb_operation_artifact`` traversal.
        """
        lineage = getattr(self, "_lineage", None)
        if lineage is None:
            from mmfdb.provenance.lineage import Lineage

            lineage = Lineage.from_connection(self.conn)
            self._lineage = lineage
        return lineage

    def connect(self):
        if self._conn is not None:
            return
        if self.database_target is None:
            raise RuntimeError("A detached borrowed connection cannot be reopened")
        self._dao = None
        self._lineage = None
        if self.database_target.dialect == "postgresql":
            self._conn = connect_postgresql(self.database_target, readonly=self.readonly)
        elif self.db_path is None:
            raise RuntimeError("SQLite database path is not configured")
        elif self.readonly and self.db_path != ":memory:":
            uri = Path(str(self.db_path)).resolve().as_uri() + "?mode=ro"
            self._conn = sqlite3.connect(uri, uri=True)
        else:
            self._conn = sqlite3.connect(str(self.db_path))
        self._owns_connection = True
        try:
            self._configure_connection()
            self._initialize_schema()
        except Exception:
            # A failed handshake/schema check must not leak a server pool or a
            # SQLite file descriptor. Borrowed handles are handled separately
            # in __init__ and remain caller-owned.
            if self._conn is not None:
                self._conn.close()
                self._conn = None
            raise

    def _configure_connection(self) -> None:
        """Apply the selected backend's connection contract."""
        if self._conn is None:
            raise RuntimeError("Database not connected")
        if connection_dialect(self._conn) == "postgresql":
            # PostgreSQL connections are opened in autocommit mode so reads do
            # not accidentally create an outer transaction that swallows later
            # repository commits. Writes still use explicit BEGIN/savepoints.
            return
        self._conn.row_factory = sqlite3.Row
        if not self.readonly:
            self._conn.execute("PRAGMA journal_mode=WAL")
        if self.enforce_foreign_keys:
            self._conn.execute("PRAGMA foreign_keys=ON")
        else:
            self._conn.execute("PRAGMA foreign_keys=OFF")
        self._conn.execute("PRAGMA busy_timeout=5000")
        if self.readonly:
            self._conn.execute("PRAGMA query_only=ON")

    def _initialize_schema(self) -> None:
        """Validate schema identity and migrate only recognized databases."""
        if self.database_target is None:
            raise RuntimeError("Database target is not configured")
        if self.database_target.dialect == "postgresql":
            current = postgres_schema_version(self.conn)  # type: ignore[arg-type]
            if current == 0:
                raise DatabaseCapabilityError(
                    "PostgreSQL MMFDB connection succeeded, but the current schema "
                    "is not provisioned. Automatic schema bootstrap is SQLite-only; "
                    f"provision MMFDB schema version {schema.SCHEMA_VERSION} before use."
                )
            if current < schema.SCHEMA_VERSION:
                raise DatabaseCapabilityError(
                    f"PostgreSQL MMFDB schema {current} is older than required schema "
                    f"{schema.SCHEMA_VERSION}. Automatic server migrations are not "
                    "implemented; migrate it through an explicit deployment step."
                )
            if current > schema.SCHEMA_VERSION:
                raise RuntimeError(
                    f"MMFDB schema {current} is newer than supported schema "
                    f"{schema.SCHEMA_VERSION}; upgrade MMFDB before opening it"
                )
            validate_postgresql_schema(self.conn)  # type: ignore[arg-type]
            return
        current = schema.get_schema_version(self.conn)
        if current > schema.SCHEMA_VERSION:
            raise RuntimeError(
                f"MMFDB schema {current} is newer than supported schema "
                f"{schema.SCHEMA_VERSION}; upgrade MMFDB before opening it"
            )

        if current == 0:
            application_tables = {
                row[0]
                for row in self.conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
                if row[0] not in {"_schema_version", "mmfdb_schema_version"}
            }
            if application_tables:
                names = ", ".join(sorted(application_tables)[:5])
                raise RuntimeError(
                    "Refusing to treat a populated unversioned database as fresh; "
                    f"import it explicitly (found: {names})"
                )
            if self.readonly:
                raise RuntimeError("Cannot initialize an empty MMFDB in read-only mode")

        if not self.readonly:
            if 0 < current < schema.SCHEMA_VERSION and self.db_path is None:
                raise RuntimeError(
                    "Schema migration requires an owned file-backed database so "
                    "MMFDB can restore a pre-migration snapshot"
                )
            backup_path = None
            if (
                0 < current < schema.SCHEMA_VERSION
                and self.db_path not in (None, ":memory:")
            ):
                backup_path = backup_database_before_migration(
                    Path(self.db_path), schema.SCHEMA_VERSION
                )
            try:
                self.migration_report = schema.migrate_schema(self.conn)
            except Exception as exc:
                self.conn.rollback()
                if backup_path is not None and self.db_path is not None:
                    self.conn.close()
                    self._conn = None
                    restore_database_backup(backup_path, Path(self.db_path))
                    if hasattr(exc, "add_note"):
                        exc.add_note(
                            f"MMFDB restored pre-migration snapshot {backup_path}"
                        )
                raise

    def close(self):
        if self._conn is not None:
            if self._owns_connection:
                self._conn.close()
            self._conn = None
            self._dao = None
            self._lineage = None

    def backup_database(self, target_path: str | os.PathLike) -> str:
        """Create a SQLite backup at ``target_path``.

        Parameters
        ----------
        target_path : str or os.PathLike
            Destination path for the copied SQLite database.

        Returns
        -------
        str
            Absolute path to the backup database.
        """
        if self.database_target is None or not self.database_target.capabilities.online_backup:
            dialect = self.database_target.dialect if self.database_target else "unknown"
            raise DatabaseCapabilityError(
                f"MMFDB-managed online backup is not supported for {dialect}; "
                "use the database server's native backup tooling"
            )
        target = Path(target_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        dest = sqlite3.connect(str(target))
        try:
            self.conn.backup(dest)
        finally:
            dest.close()
        return str(target)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False

    def _transaction(self):
        """Return a savepoint-backed transaction with rollback compensation."""
        return self._managed_transaction()

    def transaction(self):
        """Compose database writes and compensating filesystem operations."""
        return self._managed_transaction()

    @contextmanager
    def _managed_transaction(self):
        rollback_callbacks = getattr(self, "_rollback_callbacks", None)
        if rollback_callbacks is None:
            rollback_callbacks = []
            self._rollback_callbacks = rollback_callbacks
        commit_callbacks = getattr(self, "_commit_callbacks", None)
        if commit_callbacks is None:
            commit_callbacks = []
            self._commit_callbacks = commit_callbacks
        rollback_marker = len(rollback_callbacks)
        commit_marker = len(commit_callbacks)
        self._transaction_depth = getattr(self, "_transaction_depth", 0) + 1
        try:
            with _transaction(self.conn):
                yield self
        except Exception:
            pending = rollback_callbacks[rollback_marker:]
            del rollback_callbacks[rollback_marker:]
            del commit_callbacks[commit_marker:]
            for callback in reversed(pending):
                try:
                    callback()
                except Exception:
                    logger.exception("MMFDB rollback compensation failed")
            raise
        else:
            if self._transaction_depth == 1:
                pending = commit_callbacks[:]
                commit_callbacks.clear()
                for callback in pending:
                    try:
                        callback()
                    except Exception:
                        # The database commit already succeeded. Leave external
                        # cleanup to reconciliation rather than lying about the
                        # durable SQL state by raising a transaction failure.
                        logger.exception("MMFDB post-commit callback failed")
        finally:
            self._transaction_depth -= 1
            if self._transaction_depth == 0:
                rollback_callbacks.clear()
                commit_callbacks.clear()

    def _on_rollback(self, callback) -> None:
        """Register a compensation owned by the active repository transaction."""
        if getattr(self, "_transaction_depth", 0) <= 0:
            raise RuntimeError("Rollback compensation requires db.transaction()")
        self._rollback_callbacks.append(callback)

    def _on_commit(self, callback) -> None:
        """Register work that may run only after the outer SQL commit succeeds."""
        if getattr(self, "_transaction_depth", 0) <= 0:
            raise RuntimeError("Commit callback requires db.transaction()")
        self._commit_callbacks.append(callback)

    def validate_extensible_vocab(self, field_name: str, value: str | None) -> None:
        """Validate that value is active in mmfdb_vocabulary for field_name."""
        if value is None:
            return
        rows = self.dao.list(
            "mmfdb_vocabulary", filters={"field_name": field_name, "value": value},
            include_deleted=True, limit=1,
        )
        if not rows:
            raise ValueError(f"Unknown extensible vocabulary value {value!r} for field {field_name!r}")
        if not rows[0]["is_active"]:
            raise ValueError(f"Inactive extensible vocabulary value {value!r} for field {field_name!r}")

    def register_vocabulary_value(
        self,
        field_name: str,
        value: str,
        display_name: str | None = None,
        description: str | None = None,
        is_builtin: bool = False,
        is_active: bool = True,
    ) -> None:
        """Register an extensible vocabulary value."""
        with self._transaction():
            self.dao.upsert(
                "mmfdb_vocabulary",
                {
                    "field_name": field_name, "value": value,
                    "display_name": display_name or value, "description": description,
                    "is_builtin": int(is_builtin), "is_active": int(is_active),
                    "deleted_at": None,
                },
                conflict=["field_name", "value"],
            )

    # -- schema / migration --

    def create_tables(self):
        if self.database_target is None or not self.database_target.capabilities.schema_bootstrap:
            dialect = self.database_target.dialect if self.database_target else "unknown"
            raise DatabaseCapabilityError(
                f"Automatic schema bootstrap is not supported for {dialect}"
            )
        schema.create_tables(self.conn)

    def migrate(self):
        if self.database_target is None or not self.database_target.capabilities.schema_migrations:
            dialect = self.database_target.dialect if self.database_target else "unknown"
            raise DatabaseCapabilityError(
                f"Automatic schema migration is not supported for {dialect}"
            )
        schema.migrate(self.conn)

    def register_migration(self, version, description, applied_by):
        schema.register_migration(self.conn, version, description, applied_by)

    def get_schema_version(self):
        if self.database_target is not None and self.database_target.dialect == "postgresql":
            return postgres_schema_version(self.conn)  # type: ignore[arg-type]
        return schema.get_schema_version(self.conn)

    # -- materialized views --

    # -- external files --

    def add_external_file(self, file_path, file_format=None, content_type=None, file_size_bytes=None, md5=None, details=None):
        if not file_path:
            raise ValueError("file_path is required")
        file_uuid = str(uuid.uuid4())
        with self.conn:
            # Fresh uuid every call -> never conflicts, so this is a plain insert;
            # dao.insert returns the new autoincrement id (no follow-up SELECT).
            return int(self.dao.insert("ihm_external_files", {
                "reference_id": None, "file_path": str(file_path),
                "file_format": file_format, "content_type": content_type,
                "file_size_bytes": file_size_bytes, "md5": md5, "uuid": file_uuid,
                "details": details, "deleted_at": None,
            }))

    def get_external_file(self, file_id):
        return self.dao.get("ihm_external_files", file_id, include_deleted=True)

    def resolve_external_path(self, file_path: str) -> str:
        raw = str(file_path)
        if raw.startswith("~"):
            raw = self._os.path.expanduser(raw)
        if not self._os.path.isabs(raw) and self.db_path:
            raw = self._os.path.join(self._os.path.dirname(self.db_path), raw)
        return self._os.path.normpath(raw)

    # -- entities --

    # -- mmfdb operation management --

    # -- mmfdb artifact management --

    # -- artifacts linked to operations --

    # -- provenance edges (mmfdb) --

    # -- legacy fdb provenance for graph traversal (read-only) --

    # -- artifact lineage (PRD-21 Task 1; over the operation graph) --

    # -- mmfdb setup / setup definitions --

    # -- setup calibration --

    # -- analysis runs --

    # -- audit log --

    def add_audit_entry(self, action, entity_type, entity_id, user_id=None, old_values=None, new_values=None, details=None):
        details_json = details or {}
        if isinstance(details_json, dict):
            details_json = details_json.copy()
            details_json.update({"old_values": old_values, "new_values": new_values})
        return self.add_audit_log(
            action=action,
            target_type=entity_type,
            target_id=entity_id,
            operator_user_id=user_id,
            details=details_json,
        )

    def get_audit_log(self, entity_type=None, entity_id=None, action=None, limit=100):
        return self.get_audit_logs(
            action=action,
            target_type=entity_type,
            target_id=entity_id,
            limit=limit,
        )

    # -- flr sample / experiment (read-only wrappers for Core API) --

    def add_photon_stream(self, analysis_id, file_path, file_format=None, content_type=None, stream_id=None, detector_id=None, description=None, details=None):
        external_id = self.add_external_file(file_path, file_format, content_type, details=details)
        if stream_id is None:
            stream_id = f"stream_{external_id}"
        with self.conn:
            self.dao.upsert(
                "flr_photon_stream",
                {
                    "stream_id": stream_id, "analysis_id": analysis_id,
                    "external_file_id": external_id, "detector_id": detector_id,
                    "description": description, "details": details, "deleted_at": None,
                },
                conflict=["stream_id"],
            )
        return stream_id

    def set_experiment_key_value(self, experiment_id: str, key: str, value: str, details: str | None = None):
        with self.conn:
            # Upsert on UNIQUE(experiment_id, key) — PK-less table, valid conflict target.
            self.dao.upsert(
                "flr_experiment_key_value",
                {"experiment_id": experiment_id, "key": key, "value": value,
                 "details": details, "deleted_at": None},
                conflict=["experiment_id", "key"],
            )
            # An mmfdb://<type>/<id> value materializes a provenance edge.
            self._materialize_metadata_ref("experiment", experiment_id, key, value)

    def clear_experiment_key_values(self, experiment_id: str) -> None:
        self.conn.execute(
            "DELETE FROM flr_experiment_key_value WHERE experiment_id = ?",
            (experiment_id,),
        )

    def get_experiment_key_values(self, experiment_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT key, value, details FROM flr_experiment_key_value WHERE experiment_id = ? AND deleted_at IS NULL",
            (experiment_id,)
        ).fetchall()
        return [dict(row) for row in rows]

    def get_experiment_full(self, experiment_id: str) -> dict[str, Any] | None:
        row = self.get_experiment(experiment_id)
        if row is None:
            return None
        res = dict(row)
        res["data"] = self.dao.list("flr_experiment_data", filters={"experiment_id": experiment_id})
        res["key_values"] = self.get_experiment_key_values(experiment_id)
        return res

    def get_raw_data(self, raw_data_id: str) -> dict[str, Any] | None:
        return self.get_artifact(raw_data_id)

    def get_photon_streams(self, analysis_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT ps.*, ef.file_path, ef.file_format, ef.content_type, ef.file_size_bytes
               FROM flr_photon_stream ps
               LEFT JOIN ihm_external_files ef ON ef.id = ps.external_file_id
               WHERE ps.analysis_id = ? AND ps.deleted_at IS NULL
               ORDER BY ps.stream_id""",
            (analysis_id,),
        ).fetchall()

    def export_flr_cif(self, path, analysis_id: str | None = None, include_extension: bool = True):
        import io

        from mmfdb.cif_writer import CifWriter
        is_stream = isinstance(path, io.TextIOBase)

        if analysis_id is None:
            rows = self.dao.list("flr_fret_analysis", order_by="analysis_id", limit=1)
            analysis_id = rows[0]["analysis_id"] if rows else "analysis_1"

        _analysis_rows = self.dao.list(
            "flr_fret_analysis", filters={"analysis_id": analysis_id}, include_deleted=True, limit=1
        )
        analysis = _analysis_rows[0] if _analysis_rows else {}
        sample_id = analysis.get("sample_id") or analysis_id

        probes = self.dao.list("probes", order_by="probe_id")
        positions = self.dao.list("flr_poly_probe_position", order_by="id")
        sample_probes = [dict(row) for row in self.get_sample_probe_mappings(sample_id=sample_id)]
        if not sample_probes:
            first_probe = probes[0] if probes else None
            first_position = positions[0] if positions else None
            if first_probe is not None or first_position is not None:
                sample_probes = [
                    {
                        "sample_probe_id": 1,
                        "sample_id": sample_id,
                        "probe_id": first_probe.get("probe_id") if first_probe is not None else None,
                        "poly_probe_position_id": first_position.get("id") if first_position is not None else None,
                        "chromophore_name": first_probe.get("chromophore_name") if first_probe is not None else None,
                        "fluorophore_type": "unspecified",
                        "description": "ChiSurf legacy sample probe mapping",
                    }
                ]
        distances = self.dao.list(
            "flr_fret_distance_restraint", filters={"analysis_id": analysis_id}, order_by="id"
        )
        forster = self.dao.list(
            "flr_fret_forster_radius", filters={"sample_id": sample_id}, order_by="id"
        )
        metadata = self.get_analysis_metadata(analysis_id)
        streams = [dict(row) for row in self.get_photon_streams(analysis_id)]
        external_files = self.dao.list("ihm_external_files", order_by="id", include_deleted=True)
        properties = self.dao.list("optical_properties", order_by="id")
        spectra = self.dao.list("spectra", order_by="id")
        analysis_data = [dict(row) for row in self.get_analysis_data(analysis_id)]
        struct_refs = self.dao.list("struct_ref", order_by="ref_id")
        struct_ref_seqs = self.dao.list("struct_ref_seq", order_by="align_id")
        struct_ref_seq_difs = [
            dict(row)
            for row in self.conn.execute(
                "SELECT * FROM struct_ref_seq_dif WHERE deleted_at IS NULL ORDER BY pdbx_ordinal, seq_num"
            ).fetchall()
        ]

        def _array_to_text(blob, dtype=np.float64):
            if not blob:
                return ""
            try:
                return " ".join(f"{float(v):.8g}" for v in np.frombuffer(blob, dtype=dtype))
            except (ValueError, TypeError):
                # TypeError: blob is text (e.g. JSON), not a bytes buffer.
                pass
            try:
                values = json.loads(blob)
                return " ".join(f"{float(v):.8g}" for v in values)
            except (json.JSONDecodeError, TypeError, ValueError):
                return str(blob)

        def _write_content(writer):
            writer.start_block("chisurf_flr_export")

            with writer.loop(
                "_flr_probe_list",
                [
                    "probe_id",
                    "chromophore_name",
                    "reactive_probe_flag",
                    "reactive_probe_name",
                    "probe_origin",
                    "probe_link_type",
                    "fluorophore_type",
                    "chromophore_chem_descriptor_id",
                    "reactive_probe_chem_descriptor_id",
                    "chromophore_center_atom",
                    "details",
                ],
            ) as loop:
                for row in probes:
                    loop.write(
                        probe_id=row.get("probe_id"),
                        chromophore_name=row.get("chromophore_name"),
                        reactive_probe_flag=row.get("reactive_probe_flag"),
                        reactive_probe_name=row.get("reactive_probe_name"),
                        probe_origin=row.get("probe_origin"),
                        probe_link_type=row.get("probe_link_type"),
                        fluorophore_type=row.get("fluorophore_type"),
                        chromophore_chem_descriptor_id=row.get("chromophore_chem_descriptor_id"),
                        reactive_probe_chem_descriptor_id=row.get("reactive_probe_chem_descriptor_id"),
                        chromophore_center_atom=row.get("chromophore_center_atom"),
                        details=row.get("description"),
                    )

            with writer.loop(
                "_flr_poly_probe_position",
                ["id", "entity_id", "asym_id", "residue_number", "residue_name", "details"],
            ) as loop:
                for row in positions:
                    loop.write(
                        id=row.get("id"),
                        entity_id=row.get("entity_id"),
                        asym_id=row.get("asym_id"),
                        residue_number=row.get("residue_number"),
                        residue_name=row.get("residue_name"),
                        details=row.get("description"),
                    )

            with writer.loop(
                "_flr_sample_probe_details",
                [
                    "sample_probe_id",
                    "sample_id",
                    "probe_id",
                    "poly_probe_position_id",
                    "fluorophore_type",
                    "description",
                ],
            ) as loop:
                for row in sample_probes:
                    loop.write(
                        sample_probe_id=row.get("sample_probe_id"),
                        sample_id=row.get("sample_id"),
                        probe_id=row.get("probe_id"),
                        poly_probe_position_id=row.get("poly_probe_position_id"),
                        fluorophore_type=row.get("fluorophore_type"),
                        description=row.get("description"),
                    )

            with writer.loop(
                "_flr_fret_forster_radius",
                [
                    "id",
                    "donor_probe_id",
                    "acceptor_probe_id",
                    "forster_radius",
                    "forster_radius_error_plus",
                    "forster_radius_error_minus",
                    "kappa_squared_mode",
                    "refractive_index",
                    "citation_id",
                    "details",
                ],
            ) as loop:
                for row in forster:
                    loop.write(
                        id=row.get("id"),
                        donor_probe_id=row.get("donor_probe_id"),
                        acceptor_probe_id=row.get("acceptor_probe_id"),
                        forster_radius=row.get("forster_radius"),
                        forster_radius_error_plus=row.get("forster_radius_error_plus"),
                        forster_radius_error_minus=row.get("forster_radius_error_minus"),
                        kappa_squared_mode=row.get("kappa_squared_mode"),
                        refractive_index=row.get("refractive_index"),
                        citation_id=row.get("citation_id"),
                        details=row.get("details"),
                    )

            with writer.loop(
                "_flr_fret_analysis",
                [
                    "analysis_id",
                    "experiment_id",
                    "sample_id",
                    "type",
                    "method",
                    "sample_probe_id_1",
                    "sample_probe_id_2",
                    "forster_radius_id",
                    "dataset_list_id",
                    "external_file_id",
                    "software_id",
                    "details",
                ],
            ) as loop:
                loop.write(
                    analysis_id=analysis.get("analysis_id"),
                    experiment_id=analysis.get("experiment_id"),
                    sample_id=analysis.get("sample_id"),
                    type=analysis.get("type"),
                    method=analysis.get("method"),
                    sample_probe_id_1=analysis.get("sample_probe_id_1"),
                    sample_probe_id_2=analysis.get("sample_probe_id_2"),
                    forster_radius_id=analysis.get("forster_radius_id"),
                    dataset_list_id=analysis.get("dataset_list_id"),
                    external_file_id=analysis.get("external_file_id"),
                    software_id=analysis.get("software_id"),
                    details=analysis.get("details"),
                )

            with writer.loop(
                "_struct_ref",
                [
                    "ref_id",
                    "entity_id",
                    "db_name",
                    "db_code",
                    "pdbx_db_accession",
                    "pdbx_db_isoform",
                    "pdbx_seq_one_letter_code",
                    "organism",
                    "details",
                ],
            ) as loop:
                for row in struct_refs:
                    loop.write(
                        ref_id=row.get("ref_id"),
                        entity_id=row.get("entity_id"),
                        db_name=row.get("db_name"),
                        db_code=row.get("db_code"),
                        pdbx_db_accession=row.get("pdbx_db_accession"),
                        pdbx_db_isoform=row.get("pdbx_db_isoform"),
                        pdbx_seq_one_letter_code=row.get("pdbx_seq_one_letter_code"),
                        organism=row.get("organism"),
                        details=row.get("details"),
                    )

            with writer.loop(
                "_struct_ref_seq",
                [
                    "align_id",
                    "ref_id",
                    "seq_align_beg",
                    "seq_align_end",
                    "db_align_beg",
                    "db_align_end",
                    "pdbx_db_accession",
                    "details",
                ],
            ) as loop:
                for row in struct_ref_seqs:
                    loop.write(
                        align_id=row.get("align_id"),
                        ref_id=row.get("ref_id"),
                        seq_align_beg=row.get("seq_align_beg"),
                        seq_align_end=row.get("seq_align_end"),
                        db_align_beg=row.get("db_align_beg"),
                        db_align_end=row.get("db_align_end"),
                        pdbx_db_accession=row.get("pdbx_db_accession"),
                        details=row.get("details"),
                    )

            with writer.loop(
                "_struct_ref_seq_dif",
                [
                    "id",
                    "align_id",
                    "seq_num",
                    "mon_id",
                    "db_mon_id",
                    "details",
                    "pdbx_seq_db_name",
                    "pdbx_seq_db_accession_code",
                    "pdbx_ordinal",
                ],
            ) as loop:
                for row in struct_ref_seq_difs:
                    loop.write(
                        id=row.get("id"),
                        align_id=row.get("align_id"),
                        seq_num=row.get("seq_num"),
                        mon_id=row.get("mon_id"),
                        db_mon_id=row.get("db_mon_id"),
                        details=row.get("details"),
                        pdbx_seq_db_name=row.get("pdbx_seq_db_name"),
                        pdbx_seq_db_accession_code=row.get("pdbx_seq_db_accession_code"),
                        pdbx_ordinal=row.get("pdbx_ordinal"),
                    )

            sample_probe_by_probe = {
                row.get("probe_id"): row.get("sample_probe_id")
                for row in sample_probes
                if row.get("probe_id") is not None
            }
            with writer.loop(
                "_flr_fret_distance_restraint",
                [
                    "ordinal_id",
                    "id",
                    "group_id",
                    "sample_probe_id_1",
                    "sample_probe_id_2",
                    "state_id",
                    "analysis_id",
                    "distance",
                    "distance_error_plus",
                    "distance_error_minus",
                    "distance_type",
                    "population_fraction",
                    "peak_assignment_id",
                ],
            ) as loop:
                for i, row in enumerate(distances, 1):
                    loop.write(
                        ordinal_id=i,
                        id=row.get("id"),
                        group_id=1,
                        sample_probe_id_1=row.get("sample_probe_id_1")
                        or sample_probe_by_probe.get(row.get("probe_id_1")),
                        sample_probe_id_2=row.get("sample_probe_id_2")
                        or sample_probe_by_probe.get(row.get("probe_id_2")),
                        state_id=row.get("state_id"),
                        analysis_id=row.get("analysis_id") or analysis_id,
                        distance=row.get("distance"),
                        distance_error_plus=row.get("distance_error_plus"),
                        distance_error_minus=row.get("distance_error_minus"),
                        distance_type=row.get("distance_type"),
                        population_fraction=row.get("population_fraction"),
                        peak_assignment_id=row.get("peak_assignment_id"),
                    )

            with writer.loop(
                "_ihm_dataset_list", ["id", "data_type", "details", "database_hosted"]
            ) as loop:
                loop.write(
                    id=1,
                    data_type="analysis_metadata",
                    details="ChiSurf FLR analysis metadata",
                    database_hosted="no",
                )

            with writer.loop(
                "_ihm_external_files",
                [
                    "id",
                    "reference_id",
                    "file_path",
                    "file_format",
                    "content_type",
                    "file_size_bytes",
                    "md5",
                    "uuid",
                    "details",
                ],
            ) as loop:
                for external in external_files:
                    loop.write(
                        **{
                            k: external.get(k)
                            for k in [
                                "id",
                                "reference_id",
                                "file_path",
                                "file_format",
                                "content_type",
                                "file_size_bytes",
                                "md5",
                                "uuid",
                                "details",
                            ]
                        }
                    )

            if include_extension:
                with writer.loop(
                    "_chisurf_analysis_metadata", ["analysis_id", "key", "value"]
                ) as loop:
                    for key, value in sorted(metadata.items()):
                        loop.write(analysis_id=analysis_id, key=key, value=value)

                with writer.loop(
                    "_chisurf_probe_property",
                    ["probe_id", "property_name", "property_value", "unit", "details"],
                ) as loop:
                    for prop in properties:
                        loop.write(
                            **{
                                k: prop.get(k)
                                for k in [
                                    "probe_id",
                                    "property_name",
                                    "property_value",
                                    "unit",
                                    "details",
                                ]
                            }
                        )

                with writer.loop(
                    "_chisurf_probe_spectrum",
                    [
                        "probe_id",
                        "spectrum_type",
                        "wavelengths",
                        "intensity_values",
                        "wavelength_unit",
                        "intensity_unit",
                        "details",
                    ],
                ) as loop:
                    for spec in spectra:
                        loop.write(
                            probe_id=spec.get("probe_id"),
                            spectrum_type=spec.get("spectrum_type"),
                            wavelengths=_array_to_text(spec.get("wavelengths")),
                            intensity_values=_array_to_text(spec.get("intensity_values")),
                            wavelength_unit=spec.get("wavelength_unit") or "nm",
                            intensity_unit=spec.get("intensity_unit") or "normalized",
                            details=spec.get("details"),
                        )

                with writer.loop(
                    "_chisurf_photon_stream",
                    [
                        "stream_id",
                        "analysis_id",
                        "external_file_id",
                        "detector_id",
                        "description",
                        "details",
                    ],
                ) as loop:
                    for stream in streams:
                        loop.write(
                            **{
                                k: stream.get(k)
                                for k in [
                                    "stream_id",
                                    "analysis_id",
                                    "external_file_id",
                                    "detector_id",
                                    "description",
                                    "details",
                                ]
                            }
                        )

                with writer.loop(
                    "_chisurf_analysis_data",
                    [
                        "analysis_id",
                        "data_type",
                        "data_name",
                        "x_values",
                        "y_values",
                        "x_unit",
                        "y_unit",
                        "details",
                    ],
                ) as loop:
                    for data in analysis_data:
                        loop.write(
                            analysis_id=analysis_id,
                            data_type=data.get("data_type"),
                            data_name=data.get("data_name"),
                            x_values=_array_to_text(data.get("x_values")),
                            y_values=_array_to_text(data.get("y_values")),
                            x_unit=data.get("x_unit"),
                            y_unit=data.get("y_unit"),
                            details=data.get("details"),
                        )

        if is_stream:
            writer = CifWriter(path)
            _write_content(writer)
        else:
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w") as out:
                writer = CifWriter(out)
                _write_content(writer)
            return path

    def export_flr_cif_to_text(self, analysis_id: str | None = None, include_extension: bool = True) -> str:
        import io
        buffer = io.StringIO()
        self.export_flr_cif(buffer, analysis_id=analysis_id, include_extension=include_extension)
        return buffer.getvalue()

    def get_raw_data_references(self, experiment_id=None, data_type=None):
        filters: dict[str, Any] = {"artifact_kind": "raw_data"}
        if experiment_id is not None:
            filters["experiment_id"] = experiment_id
        rows = self.dao.list("mmfdb_artifact", filters=filters)
        res = []
        for r in rows:
            d = dict(r)
            meta = _json_loads(d.get("metadata_json")) or {}
            if data_type is not None:
                if meta.get("data_type") != data_type:
                    continue
            res.append(d)
        return res



    # -- optical_properties --

    def get_optical_properties(self, probe_id=None):
        if probe_id is not None:
            rows = self.conn.execute(
                "SELECT id, probe_id, property_name, property_value, unit, details FROM optical_properties WHERE probe_id = ? AND deleted_at IS NULL ORDER BY property_name, id",
                (probe_id,)
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT id, probe_id, property_name, property_value, unit, details FROM optical_properties WHERE deleted_at IS NULL ORDER BY property_name, id"
            ).fetchall()
        new_rows = []
        for r in rows:
            d = dict(r)
            d["property_id"] = d["id"]
            d["property_type"] = d["property_name"]
            d["value"] = d["property_value"]
            new_rows.append(d)
        return new_rows

    def add_optical_property(self, probe_id, property_type, value, unit=None, method=None, condition_json=None, details=None):
        extra = {}
        if method:
            extra["method"] = method
        if condition_json:
            extra["condition"] = condition_json
        details_str = details
        if extra:
            details_str = (details or "") + " " + _json_dumps(extra)
        with self.conn:
            # Identity-preserving upsert on UNIQUE(probe_id, property_name).
            self.dao.upsert(
                "optical_properties",
                {"probe_id": probe_id, "property_name": property_type,
                 "property_value": value, "unit": unit, "details": details_str,
                 "deleted_at": None},
                conflict=["probe_id", "property_name"],
            )

    # -- _decode helpers (adapted for mmfdb_* metadata columns) --

    def _decode_raw_data_row(self, row: dict[str, Any] | sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        data = dict(row)
        metadata_raw = data.pop("metadata_json", None) or data.pop("metadata", None) or data.pop("settings_json", None)
        if metadata_raw:
            data["metadata"] = _json_loads(metadata_raw)
        if "artifact_id" in data and "raw_data_id" not in data:
            data["raw_data_id"] = data["artifact_id"]
        if "data_format" in data and "data_type" not in data:
            data["data_type"] = data["data_format"]
        return data

    # -- update_processing_run_status --

    # ============================================================
    # Canonical API — matches the signatures expected by api.py,
    # pipeline.py, and the PRD specification.
    # ============================================================

    def default_runtime_environment(self) -> dict[str, Any]:
        return {
            "platform": platform.platform(),
            "processor": platform.processor(),
            "python_version": platform.python_version(),
        }

    def browse_datasets(
        self,
        scope: str = "all",
        query: str | None = None,
        kinds: list[str] | None = None,
        formats: list[str] | None = None,
        sample_id: str | None = None,
        owner_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
        study_id: str | None = None,
    ) -> dict[str, Any]:
        """Browse datasets with scope-based access control, search, and
        pagination.

        Parameters
        ----------
        scope : str, default='all'
            One of ``'own'`` (only owned by *owner_id*), ``'public'``
            (``is_public = 1``), or ``'all'`` (public + owned by *owner_id*).
        query : str, optional
            Free-text search against artifact_id, artifact_kind, data_format,
            and metadata_json.
        kinds : list of str, optional
            Only include artifacts with these artifact_kind values.
        formats : list of str, optional
            Only include artifacts with these data_format values.
        sample_id : str, optional
            Only include artifacts linked to this sample via mmfdb_edge.
        owner_id : str, optional
            User ID for ``'own'`` scope or to supplement ``'all'``.
        limit : int, default=50
            Maximum results per page.
        offset : int, default=0
            Pagination offset.

        Returns
        -------
        dict
            Keys:
              ``datasets`` : list of artifact dicts,
              ``total`` : int (total matching count),
              ``sample_counts`` : dict[str, int] (artifact count per sample).
        """
        where_clauses: list[str] = [
            "a.deleted_at IS NULL",
            "a.artifact_id NOT IN ("
            "  SELECT source_node_id FROM mmfdb_edge"
            "  WHERE source_node_type = 'artifact'"
            "    AND target_node_type = 'artifact'"
            "    AND relationship_type = 'grouped_in'"
            "    AND deleted_at IS NULL"
            ")"
        ]
        params: list[Any] = []

        # A dataset can be co-owned (mmfdb_artifact_owner). "own" matches the
        # original creator OR any co-owner so every owner sees it under Mine.
        owner_match = (
            "(a.created_by_user_id = ? OR a.artifact_id IN "
            "(SELECT artifact_id FROM mmfdb_artifact_owner "
            " WHERE user_id = ? AND deleted_at IS NULL))"
        )
        if scope == "own":
            if not owner_id:
                owner_id = ""
            where_clauses.append(owner_match)
            params.extend([owner_id, owner_id])
        elif scope == "public":
            where_clauses.append("a.is_public = 1")
        elif scope == "all":
            if owner_id:
                where_clauses.append(f"(a.is_public = 1 OR {owner_match})")
                params.extend([owner_id, owner_id])
            else:
                where_clauses.append("a.is_public = 1")

        if query:
            pattern = f"%{query}%"
            where_clauses.append(
                "(a.artifact_id LIKE ? OR a.artifact_kind LIKE ? "
                "OR a.data_format LIKE ? OR a.metadata_json LIKE ?)"
            )
            params.extend([pattern, pattern, pattern, pattern])

        if kinds:
            placeholders = ",".join("?" for _ in kinds)
            where_clauses.append(f"a.artifact_kind IN ({placeholders})")
            params.extend(kinds)

        if formats:
            # Stored data_format is the dot-less suffix (e.g. "ptu"); accept
            # callers that pass either ".ptu" or "ptu".
            norm_formats = [str(f).lstrip(".").lower() for f in formats]
            placeholders = ",".join("?" for _ in norm_formats)
            where_clauses.append(f"LOWER(a.data_format) IN ({placeholders})")
            params.extend(norm_formats)

        if sample_id:
            where_clauses.append(
                "a.artifact_id IN ( "
                "SELECT source_node_id FROM mmfdb_edge "
                "WHERE source_node_type = 'artifact' "
                "AND target_node_type = 'sample' "
                "AND target_node_id = ? AND deleted_at IS NULL"
                ")"
            )
            params.append(sample_id)

        if study_id:
            # The study facet (PRD-13): an artifact is in the study if it is a direct
            # member, or if its linked sample is a member (membership is many-to-many).
            where_clauses.append(
                "(a.artifact_id IN ("
                "  SELECT member_id FROM mmfdb_study_member "
                "  WHERE study_id = ? AND member_type = 'artifact' AND deleted_at IS NULL"
                ") OR a.artifact_id IN ("
                "  SELECT e.source_node_id FROM mmfdb_edge e "
                "  JOIN mmfdb_study_member m ON m.member_id = e.target_node_id "
                "    AND m.member_type = 'sample' AND m.study_id = ? "
                "    AND m.deleted_at IS NULL "
                "  WHERE e.source_node_type = 'artifact' "
                "    AND e.target_node_type = 'sample' AND e.deleted_at IS NULL"
                "))"
            )
            params.extend([study_id, study_id])

        where_sql = " AND ".join(where_clauses)

        count_row = self.conn.execute(
            f"SELECT COUNT(*) FROM mmfdb_artifact a WHERE {where_sql}",
            params,
        ).fetchone()
        total = count_row[0] if count_row else 0

        rows = self.conn.execute(
            f"SELECT a.*, "
            f"       o.refcount AS object_refcount, "
            f"       o.original_filename AS original_filename, "
            f"       s.description AS sample_name "
            f"FROM mmfdb_artifact a "
            f"LEFT JOIN mmfdb_object o ON o.object_uuid = a.object_uuid "
            f"LEFT JOIN mmfdb_edge e ON e.source_node_id = a.artifact_id "
            f"  AND e.source_node_type = 'artifact' "
            f"  AND e.target_node_type = 'sample' "
            f"  AND e.deleted_at IS NULL "
            f"LEFT JOIN flr_sample s ON s.sample_id = e.target_node_id "
            f"  AND s.deleted_at IS NULL "
            f"WHERE {where_sql} "
            f"ORDER BY a.created_at DESC, a.artifact_id LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()
        datasets = [dict(r) for r in rows]

        sample_counts: dict[str, int] = {}
        if datasets:
            art_ids = [d["artifact_id"] for d in datasets]
            placeholders = ",".join("?" for _ in art_ids)
            sc_rows = self.conn.execute(
                f"SELECT e.target_node_id AS sample_id, COUNT(*) AS cnt "
                f"FROM mmfdb_edge e "
                f"WHERE e.source_node_type = 'artifact' "
                f"AND e.target_node_type = 'sample' "
                f"AND e.deleted_at IS NULL "
                f"AND e.source_node_id IN ({placeholders}) "
                f"GROUP BY e.target_node_id",
                art_ids,
            ).fetchall()
            for sr in sc_rows:
                sample_counts[sr["sample_id"]] = sr["cnt"]

        return {
            "datasets": datasets,
            "total": total,
            "sample_counts": sample_counts,
        }

    def open_dataset(self, artifact_id: str) -> str:
        """Materialize a dataset artifact to a readable local file path.

        Parameters
        ----------
        artifact_id : str
            Artifact identifier.

        Returns
        -------
        str
            Local file path to the materialized dataset content.

        Raises
        ------
        KeyError
            If the artifact or its stored object is not found.
        """
        art = self.get_artifact(artifact_id)
        if art is None:
            raise KeyError(f"Artifact not found: {artifact_id}")

        object_uuid = art.get("object_uuid")
        if object_uuid:
            store = self._get_object_store()
            row = self.dao.get("mmfdb_object", object_uuid, include_deleted=True)
            if row is None:
                raise KeyError(f"Object not found for artifact: {artifact_id}")
            original_name = row["original_filename"] or artifact_id
            suffix = Path(original_name).suffix if "." in original_name else ""
            import tempfile
            tmp = tempfile.NamedTemporaryFile(
                suffix=suffix, delete=False, prefix="chisurf-ds-"
            )
            tmp.close()
            Path(tmp.name).unlink()
            return str(
                store.materialize(
                    row["content_md5"],
                    Path(tmp.name),
                    sha256=row.get("content_sha256"),
                )
            )

        file_path = art.get("file_path")
        if file_path and Path(file_path).exists():
            return str(file_path)

        # External references (e.g. a burst output directory or .bur file) carry
        # their path in metadata rather than the object store; materialize that.
        metadata = art.get("metadata") or art.get("metadata_json")
        if isinstance(metadata, str):
            try:
                metadata = _json_loads(metadata)
            except Exception:
                metadata = None
        if isinstance(metadata, dict):
            meta_path = metadata.get("path") or metadata.get("folder_path")
            if meta_path and Path(meta_path).exists():
                return str(meta_path)

        raise KeyError(
            f"Artifact {artifact_id} has no materializable content "
            f"(no object_uuid and no valid file_path)"
        )

    def _normalize_parameter_payload(self, parameter: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(parameter, dict):
            raise ValueError("parameter payload must be a mapping")
        parameter_uuid = parameter.get("parameter_uuid", parameter.get("id"))
        name = parameter.get("name")
        if not parameter_uuid or not name:
            raise ValueError("parameter_uuid and name are required")
        parameter_type = parameter.get("parameter_type", "free")
        validate_vocabulary(parameter_type, PARAMETER_TYPES, "parameter_type")
        return {
            "parameter_uuid": parameter_uuid,
            "name": name,
            "value": parameter.get("value"),
            "standard_error": parameter.get("standard_error"),
            "confidence_interval_low": parameter.get("confidence_interval_low"),
            "confidence_interval_high": parameter.get("confidence_interval_high"),
            "initial_value": parameter.get("initial_value"),
            "lower_bound": parameter.get("lower_bound"),
            "upper_bound": parameter.get("upper_bound"),
            "bounds_on": parameter.get("bounds_on", False),
            "units": parameter.get("units"),
            "parameter_type": parameter_type,
            "expression": parameter.get("expression"),
            "prior": parameter.get("prior"),
            "mapping": parameter.get("mapping"),
            "metadata": parameter.get("metadata"),
        }

    def record_parameter(
        self,
        parameter_uuid: str,
        operation_id: str,
        name: str,
        value: float | None = None,
        standard_error: float | None = None,
        confidence_interval_low: float | None = None,
        confidence_interval_high: float | None = None,
        initial_value: float | None = None,
        lower_bound: float | None = None,
        upper_bound: float | None = None,
        bounds_on: bool = False,
        units: str | None = None,
        parameter_type: str = "free",
        expression: str | None = None,
        prior: dict[str, Any] | None = None,
        mapping: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        role: str | None = None,
    ) -> str:
        validate_vocabulary(parameter_type, PARAMETER_TYPES, "parameter_type")
        now = _utc_now()
        with self._transaction():
            self.conn.execute(
                """INSERT INTO mmfdb_parameter (
                    parameter_uuid, operation_id, name, value, standard_error,
                    confidence_interval_low, confidence_interval_high, initial_value,
                    lower_bound, upper_bound, bounds_on, units, parameter_type, role,
                    expression, prior_json, mapping_json, metadata_json,
                    created_at, updated_at, deleted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(parameter_uuid) DO UPDATE SET
                    operation_id=excluded.operation_id,
                    name=excluded.name,
                    value=excluded.value,
                    standard_error=excluded.standard_error,
                    confidence_interval_low=excluded.confidence_interval_low,
                    confidence_interval_high=excluded.confidence_interval_high,
                    initial_value=excluded.initial_value,
                    lower_bound=excluded.lower_bound,
                    upper_bound=excluded.upper_bound,
                    bounds_on=excluded.bounds_on,
                    units=excluded.units,
                    parameter_type=excluded.parameter_type,
                    role=excluded.role,
                    expression=excluded.expression,
                    prior_json=excluded.prior_json,
                    mapping_json=excluded.mapping_json,
                    metadata_json=excluded.metadata_json,
                    updated_at=excluded.updated_at,
                    deleted_at=excluded.deleted_at""",
                (
                    parameter_uuid,
                    operation_id,
                    name,
                    value,
                    standard_error,
                    confidence_interval_low,
                    confidence_interval_high,
                    initial_value,
                    lower_bound,
                    upper_bound,
                    1 if bounds_on else 0,
                    units,
                    parameter_type,
                    role,
                    expression,
                    _json_dumps(prior),
                    _json_dumps(mapping),
                    _json_dumps(metadata),
                    now,
                    now,
                    None,
                ),
            )
            self.add_audit_log(
                action="create",
                target_type="parameter",
                target_id=parameter_uuid,
                details={"operation_id": operation_id, "name": name},
            )
        return parameter_uuid

    def get_parameter(self, parameter_uuid: str) -> dict[str, Any] | None:
        # PRD-26 Task 2: parameterised, schema-driven get. Look up by the
        # parameter_uuid column, not the table PK (parameter_id) — callers
        # identify parameters by their UUID.
        return self.dao.get(
            "mmfdb_parameter",
            parameter_uuid,
            pk_column="parameter_uuid",
            include_deleted=True,
        )

    def list_parameters(
        self,
        operation_id: str | None = None,
        parameter_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """List parameters with optional filters.

        Parameters
        ----------
        operation_id : str, optional
            Filter by operation identifier.
        parameter_type : str, optional
            Filter by parameter vocabulary value.

        Returns
        -------
        list of dict
            Parameter dictionaries.
        """
        filters: dict[str, Any] = {}
        if operation_id is not None:
            filters["operation_id"] = operation_id
        if parameter_type is not None:
            filters["parameter_type"] = parameter_type
        return self.dao.list("mmfdb_parameter", filters=filters or None, order_by="parameter_id")

    def add_audit_log(
        self,
        action: str,
        target_type: str,
        target_id: str,
        operator_user_id: str | None = None,
        details: dict[str, Any] | None = None,
        timestamp: str | None = None,
    ) -> int:
        if operator_user_id is None:
            from mmfdb.security.session import configured_default_user_id
            operator_user_id = configured_default_user_id()
        now = timestamp or _utc_now()
        with self._transaction():
            return int(self.dao.insert("mmfdb_audit_log", {
                "action": action, "target_type": target_type, "target_id": target_id,
                "operator_user_id": operator_user_id, "details_json": _json_dumps(details),
                "timestamp": now, "created_at": now, "updated_at": now, "deleted_at": None,
            }))

    def get_audit_logs(
        self,
        action: str | None = None,
        target_type: str | None = None,
        target_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM mmfdb_audit_log WHERE 1=1 AND deleted_at IS NULL"
        params: list[Any] = []
        if action is not None:
            query += " AND action = ?"
            params.append(action)
        if target_type is not None:
            query += " AND target_type = ?"
            params.append(target_type)
        if target_id is not None:
            query += " AND target_id = ?"
            params.append(target_id)
        query += " ORDER BY timestamp DESC, log_id DESC LIMIT ?"
        params.append(limit)
        logs = []
        for row in self.conn.execute(query, params).fetchall():
            data = dict(row)
            data["details"] = _json_loads(data.get("details_json"))
            logs.append(data)
        return logs

    # ── Legacy backward-compat stubs ───────────────────────────────────

    def add_raw_data_reference(
        self,
        raw_data_id: str | None = None,
        experiment_id: str | None = None,
        data_type: str = "PTU",
        storage_mode: str = "local_file",
        file_path: str | None = None,
        acquired_at: str | None = None,
        validation_status: str = "unvalidated",
        **kwargs,
    ) -> str:
        import uuid
        art_id = raw_data_id or f"raw_{uuid.uuid4().hex[:12]}"
        checksum = kwargs.pop("checksum", None)
        size_bytes = kwargs.pop("size_bytes", None)
        checksum_algorithm = kwargs.pop("checksum_algorithm", "sha256")
        mime_type = kwargs.pop("mime_type", None)
        row_count = kwargs.pop("row_count", None)
        return self.register_artifact(
            artifact_id=art_id,
            artifact_kind="raw_data",
            storage_mode=storage_mode,
            experiment_id=experiment_id,
            file_path=file_path,
            validation_status=validation_status,
            checksum=checksum,
            size_bytes=size_bytes,
            checksum_algorithm=checksum_algorithm,
            mime_type=mime_type,
            row_count=row_count,
            metadata={"data_type": data_type, "acquired_at": acquired_at, **kwargs},
        )


    def add_processed_data_product(
        self,
        processing_id: str | None = None,
        product_type: str | None = None,
        storage_mode: str | None = None,
        product: dict[str, Any] | None = None,
        **kwargs,
    ) -> str:
        import uuid
        prod_dict = product if product else kwargs
        pt = product_type or prod_dict.get("product_type", "processed_data")
        sm = storage_mode or prod_dict.get("storage_mode", "local_file")
        prod_id = prod_dict.get("processed_data_id") or f"prod_{uuid.uuid4().hex[:12]}"
        pid = processing_id or prod_dict.get("processing_id") or "unknown"
        with self._transaction():
            self.register_artifact(
                artifact_id=prod_id,
                artifact_kind=pt,
                storage_mode=sm,
                file_path=prod_dict.get("file_path"),
                folder_path=prod_dict.get("folder_path"),
                checksum=prod_dict.get("checksum"),
                row_count=prod_dict.get("row_count"),
                validation_status=prod_dict.get("validation_status", "unvalidated"),
                metadata=prod_dict,
            )
            self.record_operation_link(
                operation_id=pid,
                artifact_id=prod_id,
                direction="output",
            )
            self.add_audit_log(
                action="create",
                target_type="processed_data",
                target_id=prod_id,
                details={"processing_id": pid, "product_type": pt},
            )
        return prod_id

    def get_processed_data(self, data_id: str) -> dict[str, Any] | None:
        return self.get_artifact(data_id)

    def get_processed_data_products(
        self, processing_id: str | None = None, **kwargs
    ) -> list[dict[str, Any]]:
        if processing_id:
            links = self.dao.list(
                "mmfdb_operation_artifact",
                filters={"operation_id": processing_id, "direction": "output"},
            )
            return [self.get_artifact(r["artifact_id"]) for r in links if self.get_artifact(r["artifact_id"])]
        return self.list_artifacts(**kwargs)

    def _decode_processed_data_row(self, row: dict[str, Any] | None) -> dict[str, Any] | None:
        if row is None:
            return None
        row = dict(row)
        if "artifact_kind" in row and "product_type" not in row:
            row["product_type"] = row["artifact_kind"]
        if "artifact_id" in row and "processed_data_id" not in row:
            row["processed_data_id"] = row["artifact_id"]
        return row

    def trace_processed_data(self, processed_data_id: str, **kwargs) -> dict[str, Any] | None:
        product = self.get_processed_data(processed_data_id)
        if product is None:
            return None
        product = self._decode_processed_data_row(product)

        # Find the operation linked to this artifact
        op_rows = self.dao.list(
            "mmfdb_operation_artifact",
            filters={"artifact_id": processed_data_id, "direction": "output"}, limit=1,
        )
        processing_id = op_rows[0]["operation_id"] if op_rows else None

        if not processing_id:
            raise ValueError(f"No producing operation found for processed_data_id {processed_data_id!r}")

        product["processing_id"] = processing_id

        run = None
        if processing_id:
            run = self.get_processing_run_full(processing_id)

        included_edges = [
            dict(row)
            for row in self.get_provenance_edges(
                source_node_type="processed_data",
                source_node_id=processed_data_id,
                relationship_type="included_in",
            )
        ]
        return {
            "processed_data": product,
            "processing_run": run,
            "included_in": included_edges,
        }

    def add_trace_processed_data(self, processed_data_id: str, **kwargs):
        return self.get_processed_data(processed_data_id)

    def export_burst_processing_manifest(self, processing_id: str) -> dict[str, Any]:
        run = self.get_processing_run_full(processing_id)
        if run is None:
            raise KeyError(f"processing run not found: {processing_id}")
        experiment = _row_to_dict(self.get_experiment(str(run.get("experiment_id"))))
        products = [
            self._decode_processed_data_row(row)
            for row in self.get_processed_data_products(processing_id=processing_id)
        ]
        raw_data = [self._decode_raw_data_row(row) for row in run.get("input_raw_data", [])]
        edges = [
            self._decode_provenance_edge_row(row)
            for row in self.get_provenance_edges(processing_id=processing_id)
        ]
        return {
            "schema": "mmfdb.burst_processing_manifest.v1",
            "exported_at": _utc_now(),
            "experiment": experiment,
            "processing_run": run,
            "raw_data": raw_data,
            "processed_data": products,
            "provenance_edges": edges,
        }

    def register_archive_manifest(
        self,
        processing_id: str,
        manifest: dict[str, Any] | None = None,
        output_path: str | None = None,
    ) -> str:
        manifest = manifest or self.export_burst_processing_manifest(processing_id)
        data_json = _json_dumps(manifest)
        product_id = self.add_processed_data_product(
            processing_id,
            "archive_manifest",
            "local_file" if output_path else "embedded_json",
            file_path=output_path,
            mime_type="application/json",
            size_bytes=len(data_json.encode("utf-8")) if data_json else None,
            checksum=hashlib.sha256(data_json.encode("utf-8")).hexdigest()
            if data_json
            else None,
            data_json=data_json,
            validation_status="valid",
        )
        run = self.get_processing_run(processing_id)
        for product in self.get_processed_data_products(processing_id=processing_id):
            other_id = product.get("processed_data_id") or product.get("artifact_id")
            if other_id == product_id:
                continue
            self.add_provenance_edge(
                source_node_type="processed_data",
                source_node_id=other_id,
                target_node_type="processed_data",
                target_node_id=product_id,
                relationship_type="included_in",
                processing_id=processing_id,
                settings_hash=run.get("settings_hash") if run else None,
                software_version=run.get("software_version") if run else None,
                checksum_snapshot={
                    "source": product.get("checksum"),
                    "archive_manifest": hashlib.sha256(data_json.encode("utf-8")).hexdigest()
                    if data_json
                    else None,
                },
            )
        return product_id


    # ── PRD-named aliases ──────────────────────────────────────────────


    def register_experiment(self, experiment_id, **kwargs):
        return self.add_experiment(experiment_id, **kwargs)

    list_audit_logs = get_audit_logs

    def link_grouped_fits(self, local_fit_uuid: str, global_fit_uuid: str) -> None:
        local_run = self.get_analysis_run(local_fit_uuid)
        self.add_provenance_edge(
            source_node_type="analysis_run",
            source_node_id=global_fit_uuid,
            target_node_type="analysis_run",
            target_node_id=local_fit_uuid,
            relationship_type="grouped_in",
            processing_id=global_fit_uuid,
            software_version=local_run["software_version"] if local_run else None,
        )
