"""Dependency-free WSGI web administration and JSON-RPC transport.

Both surfaces call :mod:`mmfdb.admin.backend` handlers directly.  This keeps
authentication, authorization, validation, and mutations identical between
the standalone web UI and the embedded Qt admin.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import importlib.resources
import json
import logging
import os
import secrets
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from http.cookies import CookieError, SimpleCookie
from pathlib import Path
from socketserver import ThreadingMixIn
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlencode
from wsgiref.simple_server import WSGIServer, make_server

from mmfdb import api as mmfdb_api
from mmfdb.admin.backend import fluorophore_services, services
from mmfdb.admin.backend.auth_services import (
    login_handler,
    logout_handler,
    me_handler,
    sessions_list_handler,
)
from mmfdb.admin.backend.duplicate_grouping import group_duplicates
from mmfdb.admin.backend.triage_checks import run_deterministic_checks
from mmfdb.config import configured_object_store_backend
from mmfdb.repository import MFDatabase
from mmfdb.security.auth import AuthError, PermissionDenied
from mmfdb.store.database_resolver import object_store_root, resolve_database_path
from mmfdb.webadmin import optical_components as oc
from mmfdb.webadmin.object_transport import authorized_download, store_uploaded_file

LOGGER = logging.getLogger(__name__)
# JSON-RPC stays intentionally small; large blobs use the bounded streaming
# object endpoint instead of base64 expansion in this threaded process.
MAX_REQUEST_BYTES = 1024 * 1024
STREAM_CHUNK_BYTES = 64 * 1024
SESSION_COOKIE = "mmfdb_session"


@dataclass(frozen=True)
class EntityPage:
    """Declarative metadata for a generic entity table."""

    label: str
    result_key: str
    list_handler: Callable[..., dict[str, Any]]
    columns: tuple[str, ...]
    requires_auth_argument: bool = True


@dataclass(frozen=True)
class CrudField:
    """One safe, explicit field in a core-entity web form."""

    name: str
    label: str
    required: bool = False
    multiline: bool = False


@dataclass(frozen=True)
class CrudPage:
    """Shared backend handlers and form schema for one editable entity."""

    id_field: str
    payload_key: str
    get_handler: Callable[..., dict[str, Any]]
    save_handler: Callable[..., dict[str, Any]]
    delete_handler: Callable[..., dict[str, Any]]
    fields: tuple[CrudField, ...]


ENTITY_PAGES: dict[str, EntityPage] = {
    "samples": EntityPage("Samples", "samples", services.list_samples_handler,
                          ("sample_id", "description", "project_id", "measured_by_user_id")),
    "entities": EntityPage("Molecular entities", "entities", services.list_entities_handler,
                           ("entity_id", "common_name", "type", "description")),
    "probes": EntityPage("Probes", "probes", services.list_probes_handler,
                         ("probe_id", "chromophore_name", "category", "verification_status"), False),
    "experiments": EntityPage("Experiments", "experiments", services.list_experiments_handler,
                              ("experiment_id", "sample_id", "project_id", "status")),
    "devices": EntityPage("Devices", "devices", services.list_devices_handler,
                          ("device_id", "name", "device_type", "location")),
    "setups": EntityPage("Setups", "setups", services.list_setups_handler,
                         ("setup_id", "name", "instrument_type", "details")),
    "projects": EntityPage("Projects", "projects", services.list_projects_handler,
                           ("project_id", "name", "owner_user_id", "status")),
    "raw-data": EntityPage("Raw data", "raw_data", services.list_raw_data_handler,
                           ("raw_data_id", "experiment_id", "data_type", "location")),
    "processing": EntityPage("Processing", "processing", services.list_processing_handler,
                             ("processing_id", "experiment_id", "type", "status")),
    "analysis": EntityPage("Analysis", "analysis", services.list_analysis_handler,
                           ("analysis_id", "experiment_id", "type", "status")),
    "studies": EntityPage("Studies", "studies", services.list_studies_handler,
                          ("study_id", "name", "description", "is_public")),
    "protocols": EntityPage("Protocols", "protocols", services.list_protocols_handler,
                            ("protocol_id", "name", "version", "category")),
    "reagents": EntityPage("Reagent lots", "lots", services.list_reagent_lots_handler,
                           ("lot_id", "kind", "name", "lot_number", "expiry")),
    "calibrations": EntityPage("Calibrations", "calibrations", services.list_calibrations_handler,
                               ("artifact_id", "calibration_type", "value", "created_at")),
    "branches": EntityPage("Branches", "branches", services.list_branches_handler,
                           ("branch_uuid", "name", "head_operation_id", "created_by_user_id")),
    "artifacts": EntityPage("Artifacts", "artifacts", mmfdb_api.list_artifacts,
                            ("artifact_id", "artifact_kind", "storage_mode", "validation_status")),
    "operations": EntityPage("Operations", "operations", mmfdb_api.list_operations,
                             ("operation_id", "operation_type", "status", "operator_user_id")),
    "audit": EntityPage("Audit log", "logs", mmfdb_api.list_audit_logs,
                        ("log_id", "action", "target_type", "target_id", "timestamp")),
    "sessions": EntityPage("Auth sessions", "sessions", sessions_list_handler,
                           ("session_id", "user_id", "created_at", "expires_at", "revoked_at")),
}

CRUD_PAGES: dict[str, CrudPage] = {
    "samples": CrudPage(
        "sample_id", "sample", services.get_sample_handler,
        services.save_sample_handler, services.delete_sample_handler,
        (
            CrudField("sample_id", "Sample ID", True),
            CrudField("description", "Description", multiline=True),
            CrudField("project_id", "Project ID"),
            CrudField("measured_by_user_id", "Measured by user"),
            CrudField("measured_by_device_id", "Measured by device"),
            CrudField("details", "Details", multiline=True),
        ),
    ),
    "experiments": CrudPage(
        "experiment_id", "experiment", services.get_experiment_handler,
        services.save_experiment_handler, services.delete_experiment_handler,
        (
            CrudField("experiment_id", "Experiment ID", True),
            CrudField("sample_id", "Sample ID"),
            CrudField("project_id", "Project ID"),
            CrudField("type_id", "Experiment type ID"),
            CrudField("measured_by_user_id", "Measured by user"),
            CrudField("measured_by_device_id", "Measured by device"),
            CrudField("status", "Status"),
            CrudField("details", "Details", multiline=True),
        ),
    ),
    "devices": CrudPage(
        "device_id", "device", services.get_device_handler,
        services.save_device_handler, services.delete_device_handler,
        (
            CrudField("device_id", "Device ID", True),
            CrudField("name", "Name", True),
            CrudField("device_type", "Device type"),
            CrudField("model", "Model"),
            CrudField("serial_number", "Serial number"),
            CrudField("location", "Location"),
            CrudField("owner", "Owner"),
            CrudField("details", "Details", multiline=True),
        ),
    ),
    "entities": CrudPage(
        "entity_id", "entity", services.get_entity_handler,
        services.save_entity_handler, services.delete_entity_handler,
        (
            CrudField("entity_id", "Entity ID", True),
            CrudField("common_name", "Common name", True),
            CrudField("type", "Entity type", True),
            CrudField("sequence", "Sequence", multiline=True),
            CrudField("description", "Description", multiline=True),
        ),
    ),
}


class ServiceDispatcher:
    """Small standalone dispatcher implementing the backend registration seam."""

    def __init__(self) -> None:
        self.handlers: dict[str, Callable[[dict[str, Any]], Any]] = {}

    def register(self, name: str, handler: Callable[[dict[str, Any]], Any]) -> None:
        """Register *handler* under its JSON-RPC method name."""
        self.handlers[name] = handler

    def call(self, method: str, params: dict[str, Any]) -> Any:
        """Invoke a registered handler with mapping parameters."""
        try:
            handler = self.handlers[method]
        except KeyError as exc:
            raise LookupError(method) from exc
        return handler(params)


def _resource_text(folder: str, name: str) -> str:
    resource = importlib.resources.files("mmfdb.webadmin").joinpath(folder, name)
    return resource.read_text(encoding="utf-8")


def _escape(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple)):
        value = json.dumps(value, default=str, ensure_ascii=False)
    return html.escape(str(value), quote=True)


def _json_default(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"encoding": "base64", "data": base64.b64encode(value).decode("ascii")}
    if hasattr(value, "isoformat"):
        return value.isoformat()
    raise TypeError(f"{type(value).__name__} is not JSON serializable")


#: Sidebar glyphs per entity slug (falls back to a bullet for unmapped slugs).
_ENTITY_NAV_EMOJI = {
    "samples": "🧪", "entities": "🧬", "probes": "🌈", "experiments": "🔬",
    "devices": "🖥️", "setups": "⚙️", "projects": "📁", "raw-data": "🗂️",
    "processing": "🧮", "analysis": "📈", "studies": "📚", "protocols": "📋",
    "reagents": "⚗️", "calibrations": "🎯", "branches": "🌿", "artifacts": "🧩",
    "operations": "🔧", "audit": "📝", "sessions": "🔑",
}


class WebAdminApp:
    """WSGI application serving MMFDB RPC and an administrator UI."""

    def __init__(self, *, csrf_secret: bytes | None = None) -> None:
        self.csrf_secret = csrf_secret or secrets.token_bytes(32)
        self.dispatcher = ServiceDispatcher()
        services.register_services(
            self.dispatcher, deterministic_checks=run_deterministic_checks
        )
        self.base_template = _resource_text("templates", "base.html")
        self.login_template = _resource_text("templates", "login.html")

    def __call__(self, environ: dict[str, Any], start_response: Callable[..., Any]):
        """Handle one WSGI request."""
        try:
            status, headers, body = self._dispatch(environ)
        except Exception:
            LOGGER.exception("Unhandled MMFDB web request failure")
            status, headers, body = self._error_page(500, "Internal server error")
        headers.extend(self._security_headers())
        start_response(status, headers)
        return [body] if isinstance(body, bytes) else body

    def _dispatch(self, environ: dict[str, Any]) -> tuple[str, list[tuple[str, str]], Any]:
        method = str(environ.get("REQUEST_METHOD", "GET")).upper()
        path = str(environ.get("PATH_INFO") or "/")
        if path == "/health/live":
            return self._json_response(200, {"live": True, "service": "mmfdb"})
        if path in {"/healthz", "/health/ready"}:
            return self._readiness()
        if path == "/static/admin.css":
            if method != "GET":
                return self._error_page(405, "Method not allowed")
            css = _resource_text("static", "admin.css").encode("utf-8")
            return "200 OK", [("Content-Type", "text/css; charset=utf-8"),
                              ("Cache-Control", "public, max-age=3600")], css
        if path == "/static/admin.js":
            if method != "GET":
                return self._error_page(405, "Method not allowed")
            script = _resource_text("static", "admin.js").encode("utf-8")
            return "200 OK", [("Content-Type", "text/javascript; charset=utf-8"),
                              ("Cache-Control", "public, max-age=3600")], script
        if path == "/rpc":
            return self._rpc(environ, method)
        if path == "/objects" or path.startswith("/objects/"):
            return self._objects(environ, method, path)
        if path == "/login":
            return self._login(environ, method)

        token = self._cookie_token(environ)
        user = self._admin_user(token)
        if user is None:
            return self._redirect("/login")
        if path == "/logout":
            if method != "POST":
                return self._error_page(405, "Method not allowed", user=user, token=token)
            try:
                self._require_csrf(environ, token)
            except PermissionDenied as exc:
                return self._error_page(403, str(exc), user=user, token=token)
            logout_handler(auth={"token": token})
            return self._redirect("/login", clear_cookie=True)
        if path in ("/", "/overview"):
            if method != "GET":
                return self._error_page(405, "Method not allowed", user=user, token=token)
            return self._overview(user, token)
        if path == "/users":
            return self._users(environ, method, user, token)
        if path == "/object-admin":
            return self._object_admin(environ, method, user, token)
        if path == "/rpc-explorer":
            return self._rpc_explorer(environ, method, user, token)
        if path == "/optical-components":
            return self._optical_components(environ, method, user, token)
        if path == "/optical-components/action":
            return self._optical_action(environ, method, user, token)
        if path == "/optical-components/duplicates":
            return self._optical_duplicates(environ, method, user, token)
        if path.startswith("/entities/"):
            parts = path.removeprefix("/entities/").split("/", 1)
            slug = parts[0]
            edit_id = unquote(parts[1]) if len(parts) == 2 else None
            return self._entities(
                environ, method, slug, user, token, edit_id=edit_id
            )
        return self._error_page(404, "Page not found", user=user, token=token)

    def _readiness(self):
        """Probe the configured database and writable local object store."""
        checks: dict[str, dict[str, Any]] = {}
        try:
            with MFDatabase(resolve_database_path()) as db:
                db.conn.execute("SELECT 1").fetchone()
                schema_version = db.get_schema_version()
            checks["database"] = {"ok": True, "schema_version": schema_version}
        except Exception as exc:
            checks["database"] = {"ok": False, "error": type(exc).__name__}

        backend = configured_object_store_backend()
        if backend == "local":
            try:
                root = object_store_root()
                root.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(
                    prefix=".readiness-", dir=root, delete=True
                ) as probe:
                    probe.write(b"mmfdb-ready")
                    probe.flush()
                    os.fsync(probe.fileno())
                checks["object_store"] = {
                    "ok": True,
                    "backend": "local",
                    "root": str(root),
                }
            except Exception as exc:
                checks["object_store"] = {
                    "ok": False,
                    "backend": "local",
                    "error": type(exc).__name__,
                }
        else:
            # This endpoint guarantees a concrete writable probe for the local
            # deployment contract. Remote backends have provider-specific health
            # semantics and are validated when their configured client is built.
            try:
                from mmfdb.store.object_store import create_object_store

                create_object_store()
                checks["object_store"] = {"ok": True, "backend": backend}
            except Exception as exc:
                checks["object_store"] = {
                    "ok": False,
                    "backend": backend,
                    "error": type(exc).__name__,
                }
        ready = all(check["ok"] for check in checks.values())
        return self._json_response(
            200 if ready else 503,
            {"ready": ready, "service": "mmfdb", "checks": checks},
        )

    def _objects(self, environ: dict[str, Any], method: str, path: str):
        """Upload/download raw object bytes outside the small JSON-RPC envelope."""
        token = self._bearer_token(environ)
        cookie_token = self._cookie_token(environ)
        if not token and cookie_token:
            if self._admin_user(cookie_token) is None:
                return self._json_response(
                    403, {"ok": False, "error": "Administrator access required"}
                )
            if method == "POST":
                supplied = str(environ.get("HTTP_X_CSRF_TOKEN") or "")
                if not hmac.compare_digest(supplied, self._csrf_token(cookie_token)):
                    return self._json_response(
                        403, {"ok": False, "error": "CSRF check failed"}
                    )
            token = cookie_token
        if not token:
            return self._json_response(401, {"ok": False, "error": "Bearer token required"})
        auth = {"token": token}
        try:
            if path == "/objects":
                if method != "POST":
                    return self._json_response(
                        405, {"ok": False, "error": "POST required"},
                        extra_headers=[("Allow", "POST")],
                    )
                length = self._content_length(environ)
                if length is None:
                    return self._json_response(
                        411, {"ok": False, "error": "Content-Length required"}
                    )
                if length > services.MAX_OBJECT_UPLOAD_BYTES:
                    return self._json_response(
                        413, {"ok": False, "error": "Object exceeds 64 MiB limit"}
                    )
                filename = unquote(str(environ.get("HTTP_X_MMFDB_FILENAME") or "upload.bin"))
                if not filename or len(filename) > 512 or any(ord(char) < 32 for char in filename):
                    raise ValueError("Invalid X-MMFDB-Filename")
                metadata = self._upload_metadata(environ)
                temporary = self._spool_upload(environ["wsgi.input"], length)
                try:
                    result = store_uploaded_file(
                        temporary,
                        filename=filename,
                        mime_type=str(environ.get("CONTENT_TYPE") or "application/octet-stream"),
                        metadata=metadata,
                        auth=auth,
                    )
                finally:
                    temporary.unlink(missing_ok=True)
                return self._json_response(201, {"ok": True, "object": result})

            if method not in {"GET", "HEAD"}:
                return self._json_response(
                    405, {"ok": False, "error": "GET or HEAD required"},
                    extra_headers=[("Allow", "GET, HEAD")],
                )
            object_uuid = unquote(path.removeprefix("/objects/"))
            if not object_uuid or "/" in object_uuid:
                raise KeyError(object_uuid)
            object_path, info = authorized_download(object_uuid, auth=auth)
            size = int(info.get("size_bytes") or object_path.stat().st_size)
            filename = str(info.get("original_filename") or object_uuid)
            headers = [
                ("Content-Type", str(info.get("mime_type") or "application/octet-stream")),
                ("Content-Length", str(size)),
                ("Content-Disposition", f"attachment; filename*=UTF-8''{quote(filename, safe='') }"),
                ("Cache-Control", "private, no-store"),
            ]
            body: Any = b"" if method == "HEAD" else self._file_chunks(object_path)
            return "200 OK", headers, body
        except (AuthError, PermissionDenied) as exc:
            status = 401 if isinstance(exc, AuthError) else 403
            return self._json_response(status, {"ok": False, "error": str(exc)})
        except (KeyError, FileNotFoundError):
            return self._json_response(404, {"ok": False, "error": "Object not found"})
        except ValueError as exc:
            return self._json_response(400, {"ok": False, "error": str(exc)})

    def _object_admin(
        self,
        environ: dict[str, Any],
        method: str,
        user: dict[str, Any],
        token: str,
    ):
        """Browser controls for streaming object transfer and deletion."""
        auth = {"token": token}
        if method == "POST":
            try:
                self._require_csrf(environ, token)
                form = self._form(environ)
                if form.get("action") != "delete":
                    raise ValueError("Unsupported object action")
                services.delete_object_handler(
                    object_uuid=form.get("object_uuid", ""), auth=auth
                )
            except PermissionDenied as exc:
                return self._error_page(403, str(exc), user=user, token=token)
            except (AuthError, ValueError) as exc:
                return self._error_page(400, str(exc), user=user, token=token)
            return self._redirect("/object-admin")
        if method != "GET":
            return self._error_page(405, "Method not allowed", user=user, token=token)
        rows = services.list_objects_handler(auth=auth).get("objects", [])
        csrf = self._csrf_token(token)
        table_rows = "".join(
            "<tr>"
            f'<td><code>{_escape(row.get("object_uuid"))}</code></td>'
            f'<td>{_escape(row.get("original_filename"))}</td>'
            f'<td>{_escape(row.get("size_bytes"))}</td>'
            f'<td>{_escape(row.get("mime_type"))}</td>'
            f'<td><a class="button-link" href="/objects/{quote(str(row.get("object_uuid")), safe="")}">Download</a> '
            '<form method="post" class="inline">'
            f'<input type="hidden" name="csrf" value="{csrf}">'
            '<input type="hidden" name="action" value="delete">'
            f'<input type="hidden" name="object_uuid" value="{_escape(row.get("object_uuid"))}">'
            '<button class="danger" type="submit">Delete</button></form></td></tr>'
            for row in rows
        )
        if not table_rows:
            table_rows = '<tr><td colspan="5" class="empty">No objects</td></tr>'
        content = (
            '<div class="page-head"><div><p class="eyebrow">Object storage</p>'
            '<h1>Objects</h1><p>Stream files up to 64 MiB without JSON encoding.</p></div></div>'
            '<section><h2>Upload object</h2><form id="object-upload" class="form-grid">'
            f'<input type="hidden" name="csrf" value="{csrf}">'
            '<label>File<input name="file" type="file" required></label>'
            '<label>MIME type<input name="mime_type" placeholder="application/octet-stream"></label>'
            '<button type="submit">Upload</button><output aria-live="polite"></output></form></section>'
            '<section><h2>Stored objects</h2><div class="table-wrap"><table><thead><tr>'
            '<th>Object UUID</th><th>Filename</th><th>Bytes</th><th>MIME type</th><th>Actions</th>'
            f'</tr></thead><tbody>{table_rows}</tbody></table></div></section>'
        )
        return self._page("Objects", content, user, token)

    @staticmethod
    def _content_length(environ: dict[str, Any]) -> int | None:
        raw = str(environ.get("CONTENT_LENGTH") or "").strip()
        if not raw:
            return None
        try:
            length = int(raw)
        except ValueError as exc:
            raise ValueError("Invalid Content-Length") from exc
        if length < 0:
            raise ValueError("Invalid Content-Length")
        return length

    @staticmethod
    def _upload_metadata(environ: dict[str, Any]) -> dict[str, Any] | None:
        encoded = str(environ.get("HTTP_X_MMFDB_METADATA") or "")
        if not encoded:
            return None
        if len(encoded) > 16 * 1024:
            raise ValueError("Upload metadata header is too large")
        try:
            padding = "=" * (-len(encoded) % 4)
            value = json.loads(base64.urlsafe_b64decode(encoded + padding))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("X-MMFDB-Metadata must be base64url JSON") from exc
        if not isinstance(value, dict):
            raise ValueError("Upload metadata must be a JSON object")
        return value

    @staticmethod
    def _spool_upload(stream: Any, length: int) -> Path:
        fd, name = tempfile.mkstemp(prefix="mmfdb-upload-", suffix=".tmp")
        path = Path(name)
        remaining = length
        try:
            with os.fdopen(fd, "wb") as target:
                while remaining:
                    chunk = stream.read(min(STREAM_CHUNK_BYTES, remaining))
                    if not chunk:
                        raise ValueError("Upload ended before Content-Length bytes arrived")
                    target.write(chunk)
                    remaining -= len(chunk)
                target.flush()
                os.fsync(target.fileno())
            return path
        except Exception:
            path.unlink(missing_ok=True)
            raise

    @staticmethod
    def _file_chunks(path: Path):
        with path.open("rb") as source:
            while chunk := source.read(STREAM_CHUNK_BYTES):
                yield chunk

    def _rpc(self, environ: dict[str, Any], method: str):
        if method != "POST":
            return self._json_response(405, self._rpc_error(None, -32600, "POST required"),
                                       extra_headers=[("Allow", "POST")])
        try:
            payload = json.loads(self._read_body(environ).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            return self._json_response(400, self._rpc_error(None, -32700, f"Parse error: {exc}"))
        requests = payload if isinstance(payload, list) else [payload]
        if not requests:
            return self._json_response(400, self._rpc_error(None, -32600, "Invalid Request"))
        responses = [self._rpc_one(environ, request) for request in requests]
        responses = [response for response in responses if response is not None]
        if not responses:
            return "204 No Content", [], b""
        result: Any = responses if isinstance(payload, list) else responses[0]
        return self._json_response(200, result)

    def _rpc_one(self, environ: dict[str, Any], request: Any) -> dict[str, Any] | None:
        if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
            return self._rpc_error(request.get("id") if isinstance(request, dict) else None,
                                   -32600, "Invalid Request")
        request_id = request.get("id")
        notification = "id" not in request
        method = request.get("method")
        params = request.get("params", {})
        if not isinstance(method, str) or not isinstance(params, dict):
            return None if notification else self._rpc_error(request_id, -32602, "Invalid params")
        params = dict(params)
        bearer = self._bearer_token(environ)
        cookie_token = self._cookie_token(environ)
        if bearer:
            params.setdefault("auth", {"token": bearer})
        elif cookie_token:
            params.setdefault("auth", {"token": cookie_token})
            if method != "mmfdb.security.auth.login":
                supplied = str(environ.get("HTTP_X_CSRF_TOKEN") or "")
                if not hmac.compare_digest(supplied, self._csrf_token(cookie_token)):
                    return None if notification else self._rpc_error(request_id, -32002, "CSRF check failed")
        try:
            result = self.dispatcher.call(method, params)
            if isinstance(result, dict) and result.get("ok") is False and result.get("jsonrpc_code"):
                response = self._rpc_error(
                    request_id, int(result["jsonrpc_code"]), str(result.get("error", "Request failed")),
                    {key: value for key, value in result.items() if key not in {"ok", "error", "jsonrpc_code"}},
                )
            else:
                response = {"jsonrpc": "2.0", "id": request_id, "result": result}
        except LookupError:
            response = self._rpc_error(request_id, -32601, "Method not found")
        except (AuthError, PermissionDenied) as exc:
            response = self._rpc_error(request_id, -32001, str(exc))
        except (TypeError, ValueError) as exc:
            response = self._rpc_error(request_id, -32602, str(exc))
        except Exception:
            LOGGER.exception("RPC method %s failed", method)
            response = self._rpc_error(request_id, -32603, "Internal error")
        return None if notification else response

    @staticmethod
    def _rpc_error(request_id: Any, code: int, message: str,
                   data: Any | None = None) -> dict[str, Any]:
        error: dict[str, Any] = {"code": code, "message": message}
        if data:
            error["data"] = data
        return {"jsonrpc": "2.0", "id": request_id, "error": error}

    def _login(self, environ: dict[str, Any], method: str):
        if method == "GET":
            token = self._cookie_token(environ)
            if self._admin_user(token):
                return self._redirect("/overview")
            return self._login_page()
        if method != "POST":
            return self._error_page(405, "Method not allowed")
        try:
            form = self._form(environ)
            result = login_handler(
                user_id=form.get("user_id", ""), password=form.get("password", ""),
                provider=form.get("provider") or None,
                client_metadata={"client": "mmfdb-webadmin"},
            )
            user = result.get("user") or {}
            token = str(result.get("token") or "")
            if not token or not user.get("is_admin"):
                if token:
                    logout_handler(auth={"token": token})
                return self._login_page("Administrator access is required", status=403)
            return self._redirect("/overview", token=token)
        except (AuthError, ValueError) as exc:
            return self._login_page(str(exc), status=401)

    def _overview(self, user: dict[str, Any], token: str):
        status = services.status_handler(auth={"token": token})
        cards = "".join(
            f'<div class="metric"><strong>{_escape(status.get(key, 0))}</strong><span>{_escape(label)}</span></div>'
            for key, label in (
                ("sample_count", "Samples"), ("experiment_count", "Experiments"),
                ("user_count", "Users"), ("device_count", "Devices"),
                ("provenance_edge_count", "Provenance edges"),
            )
        )
        content = (
            '<section class="hero"><p class="eyebrow">System overview</p><h1>MMFDB administration</h1>'
            f'<p>Schema version <code>{_escape(status.get("schema_version"))}</code></p></section>'
            f'<section class="metrics">{cards}</section>'
            '<section><h2>Database</h2><dl>'
            f'<dt>Active database</dt><dd><code>{_escape(status.get("user_database"))}</code></dd>'
            f'<dt>Curated source</dt><dd><code>{_escape(status.get("source_database"))}</code></dd>'
            '</dl></section>'
        )
        return self._page("Overview", content, user, token)

    def _users(self, environ: dict[str, Any], method: str,
               user: dict[str, Any], token: str):
        message = ""
        if method == "POST":
            try:
                self._require_csrf(environ, token)
                form = self._form(environ)
                action = form.get("action", "save")
                if action == "delete":
                    services.delete_user_handler(form.get("user_id", ""), auth={"token": token})
                    message = "User deleted"
                else:
                    record: dict[str, Any] = {
                        "user_id": form.get("user_id", "").strip(),
                        "display_name": form.get("display_name", "").strip(),
                        "email": form.get("email", "").strip() or None,
                        "is_admin": 1 if form.get("is_admin") == "1" else 0,
                        "allow_passwordless_login": 1 if form.get("allow_passwordless_login") == "1" else 0,
                    }
                    if form.get("password"):
                        record["password"] = form["password"]
                    services.save_user_handler(record, auth={"token": token})
                    message = "User saved"
            except (AuthError, PermissionDenied, ValueError) as exc:
                message = str(exc)
        elif method != "GET":
            return self._error_page(405, "Method not allowed", user=user, token=token)
        rows = services.list_users_handler(auth={"token": token}).get("users", [])
        csrf = self._csrf_token(token)
        table_rows = "".join(
            "<tr>"
            f"<td><strong>{_escape(row.get('user_id'))}</strong></td>"
            f"<td>{_escape(row.get('display_name'))}</td><td>{_escape(row.get('email'))}</td>"
            f"<td>{'yes' if row.get('is_admin') else 'no'}</td>"
            f"<td>{_escape(row.get('auth_provider') or 'local')}</td>"
            "<td><form method=\"post\" class=\"inline\">"
            f'<input type="hidden" name="csrf" value="{csrf}"><input type="hidden" name="action" value="delete">'
            f'<input type="hidden" name="user_id" value="{_escape(row.get("user_id"))}">'
            '<button class="danger" type="submit">Delete</button></form></td></tr>'
            for row in rows
        )
        content = (
            '<div class="page-head"><div><p class="eyebrow">Identity</p><h1>Users</h1></div></div>'
            + (f'<p class="notice">{_escape(message)}</p>' if message else "")
            + '<section><h2>Add or update a user</h2><form method="post" class="form-grid">'
              f'<input type="hidden" name="csrf" value="{csrf}">'
              '<label>User ID<input name="user_id" required maxlength="128"></label>'
              '<label>Display name<input name="display_name" required maxlength="256"></label>'
              '<label>Email<input name="email" type="email" maxlength="320"></label>'
              '<label>Password<input name="password" type="password" autocomplete="new-password"></label>'
              '<label class="check"><input name="is_admin" type="checkbox" value="1"> Administrator</label>'
              '<label class="check"><input name="allow_passwordless_login" type="checkbox" value="1"> Passwordless login</label>'
              '<button type="submit">Save user</button></form></section>'
            + '<section><h2>Accounts</h2><div class="table-wrap"><table><thead><tr>'
              '<th>User ID</th><th>Name</th><th>Email</th><th>Admin</th><th>Provider</th><th></th>'
              f'</tr></thead><tbody>{table_rows}</tbody></table></div></section>'
        )
        return self._page("Users", content, user, token)

    def _entities(
        self,
        environ: dict[str, Any],
        method: str,
        slug: str,
        user: dict[str, Any],
        token: str,
        *,
        edit_id: str | None = None,
    ):
        page = ENTITY_PAGES.get(slug)
        if page is None:
            return self._error_page(404, "Entity page not found", user=user, token=token)
        crud = CRUD_PAGES.get(slug)
        auth = {"token": token}
        if method == "POST":
            if crud is None or edit_id is not None:
                return self._error_page(405, "Method not allowed", user=user, token=token)
            try:
                self._require_csrf(environ, token)
                form = self._form(environ)
                record_id = form.get(crud.id_field, "").strip()
                if not record_id:
                    raise ValueError(f"{crud.id_field} is required")
                action = form.get("action", "save")
                if action == "delete":
                    crud.delete_handler(**{crud.id_field: record_id, "auth": auth})
                elif action == "save":
                    current_result = crud.get_handler(
                        **{crud.id_field: record_id, "auth": auth}
                    )
                    payload = dict(current_result.get(crud.payload_key) or {})
                    payload.update(
                        {
                            field.name: form.get(field.name, "").strip()
                            for field in crud.fields
                            if field.name in form
                        }
                    )
                    if slug == "entities" and payload.get("sequence"):
                        raw_sequence = str(payload["sequence"])
                        tokens = raw_sequence.replace(",", " ").split()
                        payload["sequence"] = tokens if len(tokens) > 1 else list(raw_sequence)
                    crud.save_handler(**{crud.payload_key: payload, "auth": auth})
                else:
                    raise ValueError("Unsupported entity action")
            except PermissionDenied as exc:
                return self._error_page(403, str(exc), user=user, token=token)
            except AuthError as exc:
                return self._error_page(401, str(exc), user=user, token=token)
            except ValueError as exc:
                return self._error_page(400, str(exc), user=user, token=token)
            return self._redirect(f"/entities/{slug}")
        if method != "GET":
            return self._error_page(405, "Method not allowed", user=user, token=token)

        editing: dict[str, Any] = {}
        if edit_id is not None:
            if crud is None:
                return self._error_page(404, "Editing is unavailable", user=user, token=token)
            result = crud.get_handler(**{crud.id_field: edit_id, "auth": auth})
            editing = dict(result.get(crud.payload_key) or {})
            if not editing:
                return self._error_page(404, "Record not found", user=user, token=token)
        kwargs = {"auth": {"token": token}} if page.requires_auth_argument else {}
        result = page.list_handler(**kwargs)
        rows = result.get(page.result_key, []) or []
        cells = "".join(f"<th>{_escape(column.replace('_', ' ').title())}</th>" for column in page.columns)
        csrf = self._csrf_token(token)
        body_parts: list[str] = []
        for row in rows:
            row_dict = dict(row)
            cells_html = "".join(
                f"<td>{_escape(row_dict.get(column))}</td>" for column in page.columns
            )
            actions = ""
            if crud is not None:
                record_id = str(row_dict.get(crud.id_field) or "")
                actions = (
                    f'<td><a class="button-link" href="/entities/{slug}/{quote(record_id, safe="")}">Edit</a> '
                    '<form method="post" class="inline">'
                    f'<input type="hidden" name="csrf" value="{csrf}">'
                    '<input type="hidden" name="action" value="delete">'
                    f'<input type="hidden" name="{_escape(crud.id_field)}" value="{_escape(record_id)}">'
                    '<button class="danger" type="submit">Delete</button></form></td>'
                )
            body_parts.append(f"<tr>{cells_html}{actions}</tr>")
        body = "".join(body_parts)
        column_count = len(page.columns) + (1 if crud else 0)
        if not body:
            body = f'<tr><td colspan="{column_count}" class="empty">No records</td></tr>'
        form_html = ""
        if crud is not None:
            fields_html = "".join(
                self._crud_input(field, editing, readonly=bool(editing) and field.name == crud.id_field)
                for field in crud.fields
            )
            mode = "Edit" if editing else "Create"
            form_html = (
                f'<section><h2>{mode} {html.escape(page.label.lower())}</h2>'
                '<form method="post" action="/entities/' + slug + '" class="form-grid">'
                f'<input type="hidden" name="csrf" value="{csrf}">'
                '<input type="hidden" name="action" value="save">'
                f'{fields_html}<button type="submit">Save</button></form></section>'
            )
        content = (
            f'<div class="page-head"><div><p class="eyebrow">Database entities</p><h1>{_escape(page.label)}</h1>'
            f'<p>{len(rows)} record(s)</p></div></div>{form_html}<section><div class="table-wrap"><table>'
            f'<thead><tr>{cells}{"<th>Actions</th>" if crud else ""}</tr></thead>'
            f'<tbody>{body}</tbody></table></div></section>'
        )
        return self._page(page.label, content, user, token)

    @staticmethod
    def _crud_input(field: CrudField, record: dict[str, Any], *, readonly: bool) -> str:
        value = record.get(field.name, "")
        if isinstance(value, list):
            value = " ".join(str(item) for item in value)
        attributes = " required" if field.required else ""
        if readonly:
            attributes += " readonly"
        if field.multiline:
            control = (
                f'<textarea name="{_escape(field.name)}" rows="4"{attributes}>'
                f'{_escape(value)}</textarea>'
            )
        else:
            control = (
                f'<input name="{_escape(field.name)}" value="{_escape(value)}"'
                f'{attributes} maxlength="512">'
            )
        return f'<label>{_escape(field.label)}{control}</label>'

    def _rpc_explorer(self, environ: dict[str, Any], method: str,
                      user: dict[str, Any], token: str):
        """Render and execute registered methods through the shared dispatcher."""
        result_html = ""
        selected = ""
        params_text = "{}"
        if method == "POST":
            try:
                self._require_csrf(environ, token)
                form = self._form(environ)
                selected = form.get("rpc_method", "")
                params_text = form.get("params", "{}")
                params = json.loads(params_text)
                if not isinstance(params, dict):
                    raise ValueError("Parameters must be a JSON object")
                params.setdefault("auth", {"token": token})
                result = self.dispatcher.call(selected, params)
                rendered = json.dumps(result, indent=2, default=_json_default, ensure_ascii=False)
                result_html = f'<section><h2>Result</h2><pre>{_escape(rendered)}</pre></section>'
            except (LookupError, AuthError, PermissionDenied, ValueError, TypeError) as exc:
                result_html = f'<p class="notice">{_escape(exc)}</p>'
        elif method != "GET":
            return self._error_page(405, "Method not allowed", user=user, token=token)
        options = "".join(
            f'<option value="{_escape(name)}"' + (" selected" if name == selected else "") +
            f'>{_escape(name)}</option>'
            for name in sorted(self.dispatcher.handlers)
        )
        content = (
            '<div class="page-head"><div><p class="eyebrow">Developer administration</p>'
            '<h1>RPC explorer</h1><p>Invoke any registered MMFDB backend operation.</p></div></div>'
            '<section><form method="post" class="rpc-form">'
            f'<input type="hidden" name="csrf" value="{self._csrf_token(token)}">'
            f'<label>Method<select name="rpc_method" required>{options}</select></label>'
            f'<label>Parameters (JSON object)<textarea name="params" rows="12">{_escape(params_text)}</textarea></label>'
            '<button type="submit">Invoke method</button></form></section>' + result_html
        )
        return self._page("RPC explorer", content, user, token)

    # ------------------------------------------------------------------
    # Optical-component curation (browser port of the Qt OpticalComponentDock)
    # ------------------------------------------------------------------

    #: Cap on how many probes are fetched+overlaid in one spectrum plot.
    _SPECTRA_PLOT_LIMIT = 12

    @staticmethod
    def _query_params(environ: dict[str, Any]) -> dict[str, list[str]]:
        return parse_qs(str(environ.get("QUERY_STRING") or ""), keep_blank_values=False)

    @staticmethod
    def _int_list(values: list[str]) -> list[int]:
        result: list[int] = []
        for value in values:
            for token in str(value).replace(",", " ").split():
                try:
                    result.append(int(token))
                except ValueError:
                    continue
        return result

    def _probe_details(self, probe_ids: list[int], auth: dict[str, Any]) -> list[dict[str, Any]]:
        """Fetch full probe+spectra detail for each id (best-effort, order preserved)."""
        details: list[dict[str, Any]] = []
        for pid in probe_ids[: self._SPECTRA_PLOT_LIMIT]:
            try:
                details.append(fluorophore_services.handle_get_probe(pid, auth=auth))
            except (ValueError, KeyError):
                continue
        return details

    def _optical_components(self, environ: dict[str, Any], method: str,
                            user: dict[str, Any], token: str):
        if method != "GET":
            return self._error_page(405, "Method not allowed", user=user, token=token)
        auth = {"token": token}
        params = self._query_params(environ)
        component = oc.component_for((params.get("ct") or [None])[0])
        status = (params.get("status") or ["all"])[0]
        if status not in oc.STATUS_CHOICES:
            status = "all"
        search = (params.get("search") or [""])[0].strip()
        sel = self._int_list(params.get("sel", []))
        probe_values = self._int_list(params.get("probe", []))
        probe = probe_values[0] if probe_values else None
        message = (params.get("msg") or [""])[0]

        listing = fluorophore_services.handle_list_probes(
            verification_status=None if status == "all" else status,
            category=component["categories"],
            search=search or None,
            limit=500,
            auth=auth,
        )
        rows = listing.get("probes", [])
        total = listing.get("total", len(rows))

        detail = None
        if probe is not None:
            try:
                got = fluorophore_services.handle_get_probe(probe, auth=auth)
                detail = oc.merge_detail(got.get("probe", {}), got.get("optical_properties", []))
            except (ValueError, KeyError):
                detail = None

        if sel:
            spectra_details = self._probe_details(sel, auth)
        elif probe is not None:
            spectra_details = self._probe_details([probe], auth)
        else:
            spectra_details = []

        content = oc.render_page(
            component=component, rows=rows, total=total, status=status, search=search,
            sel=sel, probe=probe, spectra_details=spectra_details, detail=detail,
            message=message, csrf=self._csrf_token(token),
        )
        return self._page(f"{component['label']}", content, user, token)

    def _optical_action(self, environ: dict[str, Any], method: str,
                        user: dict[str, Any], token: str):
        if method != "POST":
            return self._error_page(405, "Method not allowed", user=user, token=token)
        auth = {"token": token}
        form = parse_qs(self._read_body(environ).decode("utf-8"), keep_blank_values=True)
        environ["mmfdb.form"] = {k: v[-1] for k, v in form.items()}
        try:
            self._require_csrf(environ, token)
        except PermissionDenied as exc:
            return self._error_page(403, str(exc), user=user, token=token)
        ct = (form.get("ct") or [oc.DEFAULT_COMPONENT_KEY])[0]
        status = (form.get("status") or ["all"])[0]
        search = (form.get("search") or [""])[0]
        sel = self._int_list(form.get("sel", []))
        op = (form.get("op") or [""])[0]

        message = ""
        try:
            if op == "defaults":
                counts = fluorophore_services.handle_import_default_spectra(auth=auth)
                message = (f"🧪 Loaded ChiSurf defaults: {counts.get('probes', 0)} new probe(s), "
                           f"{counts.get('spectra', 0)} spectra, "
                           f"{counts.get('optical_properties', 0)} optical properties")
            elif op == "import":
                counts = fluorophore_services.handle_import_reference_set(
                    mark_verified=False, auth=auth
                )
                message = (f"📥 Imported {counts.get('probes', 0)} probes, "
                           f"{counts.get('spectra', 0)} spectra, "
                           f"{counts.get('optical_properties', 0)} optical properties")
            elif op == "approve":
                for pid in sel:
                    fluorophore_services.handle_approve_probe(pid, verified_by=user.get("user_id", "admin"), auth=auth)
                message = f"✅ Approved {len(sel)} item(s)"
            elif op == "reject":
                for pid in sel:
                    fluorophore_services.handle_reject_probe(pid, verified_by=user.get("user_id", "admin"), auth=auth)
                message = f"❌ Rejected {len(sel)} item(s)"
            elif op == "triage":
                total_issues = 0
                for pid in sel:
                    result = fluorophore_services.handle_run_ai_triage(
                        pid, auth=auth, deterministic_checks=run_deterministic_checks
                    )
                    total_issues += len(result.get("issues", []))
                message = f"🤖 Triaged {len(sel)} item(s); {total_issues} issue(s) flagged, queued for review"
            else:
                message = "Unknown action"
        except (AuthError, PermissionDenied, ValueError) as exc:
            message = f"⚠️ {exc}"

        query = [("ct", ct)]
        if status != "all":
            query.append(("status", status))
        if search:
            query.append(("search", search))
        for pid in sel:
            query.append(("sel", str(pid)))
        query.append(("msg", message))
        return self._redirect("/optical-components?" + urlencode(query))

    def _optical_duplicates(self, environ: dict[str, Any], method: str,
                            user: dict[str, Any], token: str):
        auth = {"token": token}
        if method == "POST":
            form = parse_qs(self._read_body(environ).decode("utf-8"), keep_blank_values=True)
            environ["mmfdb.form"] = {k: v[-1] for k, v in form.items()}
            try:
                self._require_csrf(environ, token)
            except PermissionDenied as exc:
                return self._error_page(403, str(exc), user=user, token=token)
            ct = (form.get("ct") or [oc.DEFAULT_COMPONENT_KEY])[0]
            merged, errors = 0, 0
            for g_idx in self._int_list(form.get("merge", [])):
                ids = self._int_list(form.get(f"ids_{g_idx}", []))
                primary = self._int_list(form.get(f"primary_{g_idx}", []))
                if not primary or not ids:
                    continue
                primary_id = primary[0]
                duplicate_ids = [pid for pid in ids if pid != primary_id]
                if not duplicate_ids:
                    continue
                try:
                    fluorophore_services.handle_merge_probes(primary_id, duplicate_ids, auth=auth)
                    merged += 1
                except (AuthError, PermissionDenied, ValueError):
                    errors += 1
            message = f"🔗 Merged {merged} group(s)" + (f", {errors} failed" if errors else "")
            return self._redirect(
                "/optical-components/duplicates?" + urlencode([("ct", ct), ("msg", message)])
            )
        if method != "GET":
            return self._error_page(405, "Method not allowed", user=user, token=token)

        params = self._query_params(environ)
        ct = (params.get("ct") or [oc.DEFAULT_COMPONENT_KEY])[0]
        component = oc.component_for(ct)
        message = (params.get("msg") or [""])[0]
        found = fluorophore_services.handle_find_duplicates(auth=auth)
        probes = [p for p in found.get("probes", [])
                  if p.get("category") in component["categories"]]
        groups = group_duplicates(probes)[:60]

        spectra_by_group: dict[int, list[dict[str, Any]]] = {}
        for g_idx, group in enumerate(groups):
            probe_ids = [p["probe_id"] for p in group["probes"]]
            try:
                batch = fluorophore_services.handle_get_spectra_batch(probe_ids, auth=auth)
            except (ValueError, KeyError):
                continue
            by_probe: dict[int, dict[str, Any]] = {}
            for spec in batch.get("spectra", []):
                pid = spec.get("probe_id")
                entry = by_probe.setdefault(pid, {"probe": {}, "spectra": []})
                entry["spectra"].append({
                    "spectrum_type": spec.get("spectrum_type", ""),
                    "wavelengths": spec.get("wavelengths", []),
                    "intensity": spec.get("intensity_values", []),
                })
            for probe in group["probes"]:
                if probe["probe_id"] in by_probe:
                    by_probe[probe["probe_id"]]["probe"] = probe
            spectra_by_group[g_idx] = list(by_probe.values())

        content = oc.render_duplicates_page(
            ct=ct, groups=groups, spectra_by_group=spectra_by_group,
            message=message, csrf=self._csrf_token(token),
        )
        return self._page("Duplicates", content, user, token)

    def _page(self, title: str, content: str, user: dict[str, Any], token: str,
              *, status: int = 200):
        nav_entities = "".join(
            f'<a href="/entities/{slug}">{_ENTITY_NAV_EMOJI.get(slug, "•")} {_escape(page.label)}</a>'
            for slug, page in ENTITY_PAGES.items()
        )
        csrf = self._csrf_token(token)
        body = self.base_template.replace("{{title}}", _escape(title))
        body = body.replace("{{content}}", content).replace("{{entity_nav}}", nav_entities)
        body = body.replace("{{user}}", _escape(user.get("display_name") or user.get("user_id")))
        body = body.replace("{{csrf}}", csrf)
        return self._html_response(status, body)

    def _login_page(self, error: str = "", *, status: int = 200):
        body = self.login_template.replace("{{error}}", _escape(error))
        body = body.replace("{{error_class}}", "" if error else "hidden")
        return self._html_response(status, body)

    def _error_page(self, status: int, message: str, *, user: dict[str, Any] | None = None,
                    token: str = ""):
        content = f'<section class="error"><h1>{status}</h1><p>{_escape(message)}</p></section>'
        if user:
            return self._page(str(status), content, user, token, status=status)
        return self._html_response(status, f"<!doctype html><title>{status}</title>{content}")

    @staticmethod
    def _status(status: int) -> str:
        labels = {200: "OK", 201: "Created", 204: "No Content", 302: "Found",
                  400: "Bad Request", 401: "Unauthorized", 403: "Forbidden",
                  404: "Not Found", 405: "Method Not Allowed",
                  411: "Length Required", 413: "Content Too Large",
                  500: "Internal Server Error", 503: "Service Unavailable"}
        return f"{status} {labels.get(status, 'Error')}"

    def _html_response(self, status: int, body: str):
        return self._status(status), [("Content-Type", "text/html; charset=utf-8"),
                                     ("Cache-Control", "no-store")], body.encode("utf-8")

    def _json_response(self, status: int, value: Any,
                       *, extra_headers: list[tuple[str, str]] | None = None):
        body = json.dumps(value, default=_json_default, ensure_ascii=False).encode("utf-8")
        headers = [("Content-Type", "application/json; charset=utf-8"),
                   ("Cache-Control", "no-store")]
        headers.extend(extra_headers or [])
        return self._status(status), headers, body

    def _redirect(self, location: str, *, token: str | None = None,
                  clear_cookie: bool = False):
        headers = [("Location", location), ("Cache-Control", "no-store")]
        if token is not None:
            headers.append(("Set-Cookie", self._session_cookie(token)))
        elif clear_cookie:
            headers.append(("Set-Cookie", self._session_cookie("", max_age=0)))
        return "302 Found", headers, b""

    @staticmethod
    def _security_headers() -> list[tuple[str, str]]:
        return [
            ("Content-Security-Policy", "default-src 'self'; style-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"),
            ("X-Content-Type-Options", "nosniff"), ("X-Frame-Options", "DENY"),
            ("Referrer-Policy", "no-referrer"),
        ]

    @staticmethod
    def _read_body(environ: dict[str, Any]) -> bytes:
        raw_length = str(environ.get("CONTENT_LENGTH") or "0")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise ValueError("Invalid Content-Length") from exc
        if length < 0 or length > MAX_REQUEST_BYTES:
            raise ValueError("Request body too large")
        return environ["wsgi.input"].read(length)

    def _form(self, environ: dict[str, Any]) -> dict[str, str]:
        cached = environ.get("mmfdb.form")
        if isinstance(cached, dict):
            return cached
        content_type = str(environ.get("CONTENT_TYPE") or "").split(";", 1)[0].strip().lower()
        if content_type != "application/x-www-form-urlencoded":
            raise ValueError("Expected a form-encoded request")
        parsed = parse_qs(self._read_body(environ).decode("utf-8"), keep_blank_values=True)
        form = {key: values[-1] for key, values in parsed.items()}
        environ["mmfdb.form"] = form
        return form

    @staticmethod
    def _bearer_token(environ: dict[str, Any]) -> str:
        authorization = str(environ.get("HTTP_AUTHORIZATION") or "")
        scheme, _, token = authorization.partition(" ")
        return token.strip() if scheme.lower() == "bearer" else ""

    @staticmethod
    def _cookie_token(environ: dict[str, Any]) -> str:
        cookie = SimpleCookie()
        try:
            cookie.load(str(environ.get("HTTP_COOKIE") or ""))
        except CookieError:
            return ""
        morsel = cookie.get(SESSION_COOKIE)
        return morsel.value if morsel else ""

    @staticmethod
    def _session_cookie(token: str, *, max_age: int | None = None) -> str:
        value = f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict"
        if os.environ.get("MMFDB_WEB_SECURE_COOKIE", "").lower() in {"1", "true", "yes"}:
            value += "; Secure"
        if max_age is not None:
            value += f"; Max-Age={max_age}"
        return value

    def _csrf_token(self, session_token: str) -> str:
        return hmac.new(self.csrf_secret, session_token.encode("utf-8"), hashlib.sha256).hexdigest()

    def _require_csrf(self, environ: dict[str, Any], token: str) -> None:
        form = self._form(environ)
        if not hmac.compare_digest(form.get("csrf", ""), self._csrf_token(token)):
            raise PermissionDenied("CSRF check failed")

    @staticmethod
    def _admin_user(token: str) -> dict[str, Any] | None:
        if not token:
            return None
        try:
            user = me_handler(auth={"token": token}).get("user") or {}
        except (AuthError, PermissionDenied):
            return None
        return user if user.get("is_admin") else None


def create_app() -> WebAdminApp:
    """Create a standalone MMFDB WSGI application."""
    return WebAdminApp()


class ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
    """Thread-per-request server so one slow client cannot stall MMFDB."""

    daemon_threads = True


def serve(*, host: str = "127.0.0.1", port: int = 8080) -> None:
    """Serve MMFDB until interrupted using a concurrent stdlib WSGI server.

    Production deployments should terminate TLS and set request/time limits at
    a reverse proxy; the application itself remains dependency-free.
    """
    with make_server(host, port, create_app(), server_class=ThreadingWSGIServer) as server:
        LOGGER.info("MMFDB listening on http://%s:%s", host, port)
        server.serve_forever()
