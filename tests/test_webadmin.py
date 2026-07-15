"""Black-box WSGI tests for the standalone MMFDB web administration surface."""

from __future__ import annotations

import base64
import io
import json
import re
from urllib.parse import urlencode

import pytest
from mmfdb.admin.backend.services import MAX_OBJECT_UPLOAD_BYTES
from mmfdb.config import configure_runtime, reset_runtime_config
from mmfdb.repository import MFDatabase
from mmfdb.security.bootstrap import bootstrap_local_admin
from mmfdb.webadmin import MAX_REQUEST_BYTES, WebAdminApp, create_app


@pytest.fixture
def webadmin(tmp_path):
    database_path = tmp_path / "webadmin.db"
    configure_runtime(database_path=database_path, settings_dir=tmp_path)
    with MFDatabase(database_path) as db:
        bootstrap_local_admin(
            db.conn, user_id="admin", password="Admin123!", commit=True
        )
    yield create_app()
    reset_runtime_config()


def request(
    app: WebAdminApp,
    path: str,
    *,
    method: str = "GET",
    form: dict[str, str] | None = None,
    json_body: object | None = None,
    cookie: str = "",
    bearer: str = "",
    csrf: str = "",
    body: bytes | None = None,
    content_type: str | None = None,
    headers: dict[str, str] | None = None,
    content_length: int | None = None,
):
    if form is not None:
        body = urlencode(form).encode()
        content_type = "application/x-www-form-urlencoded"
    elif json_body is not None:
        body = json.dumps(json_body).encode()
        content_type = "application/json"
    elif body is None:
        body = b""
        content_type = content_type or ""
    else:
        content_type = content_type or "application/octet-stream"
    environ = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "CONTENT_TYPE": content_type,
        "CONTENT_LENGTH": str(len(body) if content_length is None else content_length),
        "wsgi.input": io.BytesIO(body),
        "HTTP_COOKIE": cookie,
    }
    if bearer:
        environ["HTTP_AUTHORIZATION"] = f"Bearer {bearer}"
    if csrf:
        environ["HTTP_X_CSRF_TOKEN"] = csrf
    for key, value in (headers or {}).items():
        environ["HTTP_" + key.upper().replace("-", "_")] = value
    response: dict[str, object] = {}

    def start_response(status, headers):
        response["status"] = status
        response["headers"] = headers

    chunks = list(app(environ, start_response))
    response["chunks"] = chunks
    response["body"] = b"".join(chunks)
    return response


def header(response, name: str) -> str:
    return next(value for key, value in response["headers"] if key.lower() == name.lower())


def rpc(app, method: str, params: dict | None = None, *, bearer: str = ""):
    response = request(
        app,
        "/rpc",
        method="POST",
        bearer=bearer,
        json_body={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
    )
    return response, json.loads(response["body"])


def login(app: WebAdminApp):
    response = request(
        app,
        "/login",
        method="POST",
        form={"user_id": "admin", "password": "Admin123!"},
    )
    assert response["status"] == "302 Found"
    cookie = header(response, "Set-Cookie").split(";", 1)[0]
    return cookie, cookie.split("=", 1)[1]


def test_health_assets_and_security_headers_are_public(webadmin):
    health = request(webadmin, "/healthz")
    payload = json.loads(health["body"])
    assert payload["ready"] is True
    assert payload["checks"]["database"]["ok"] is True
    assert payload["checks"]["object_store"]["ok"] is True
    assert header(health, "X-Frame-Options") == "DENY"
    assert json.loads(request(webadmin, "/health/live")["body"]) == {
        "live": True,
        "service": "mmfdb",
    }
    css = request(webadmin, "/static/admin.css")
    assert css["status"] == "200 OK"
    assert b"--accent" in css["body"]
    script = request(webadmin, "/static/admin.js")
    assert script["status"] == "200 OK"
    assert b'fetch("/objects"' in script["body"]


def test_browser_admin_requires_an_admin_session(webadmin):
    response = request(webadmin, "/overview")
    assert response["status"] == "302 Found"
    assert header(response, "Location") == "/login"

    bad = request(
        webadmin, "/login", method="POST",
        form={"user_id": "admin", "password": "wrong"},
    )
    assert bad["status"] == "401 Unauthorized"
    assert b"Invalid credentials" in bad["body"]


def test_overview_users_entity_breadth_and_rpc_explorer(webadmin):
    cookie, _ = login(webadmin)
    for path, expected in (
        ("/overview", b"MMFDB administration"),
        ("/users", b"Add or update a user"),
        ("/entities/samples", b"Samples"),
        ("/entities/experiments", b"Experiments"),
        ("/entities/setups", b"Setups"),
        ("/entities/studies", b"Studies"),
        ("/entities/protocols", b"Protocols"),
        ("/entities/reagents", b"Reagent lots"),
        ("/entities/calibrations", b"Calibrations"),
        ("/entities/projects", b"Projects"),
        ("/entities/artifacts", b"Artifacts"),
        ("/entities/operations", b"Operations"),
        ("/entities/audit", b"Audit log"),
        ("/entities/sessions", b"Auth sessions"),
        ("/rpc-explorer", b"mmfdb.security.auth.login"),
    ):
        response = request(webadmin, path, cookie=cookie)
        assert response["status"] == "200 OK", path
        assert expected in response["body"], path
    assert len(webadmin.dispatcher.handlers) >= 100

    explorer = request(webadmin, "/rpc-explorer", cookie=cookie)
    csrf = re.search(rb'name="csrf" value="([a-f0-9]+)"', explorer["body"]).group(1).decode()
    invoked = request(
        webadmin,
        "/rpc-explorer",
        method="POST",
        cookie=cookie,
        form={"csrf": csrf, "rpc_method": "mmfdb.status", "params": "{}"},
    )
    assert invoked["status"] == "200 OK"
    assert b'&quot;schema_version&quot;' in invoked["body"]


def test_user_mutations_require_csrf_and_escape_output(webadmin):
    cookie, _ = login(webadmin)
    users = request(webadmin, "/users", cookie=cookie)
    csrf = re.search(rb'name="csrf" value="([a-f0-9]+)"', users["body"]).group(1).decode()
    new_user = {
        "csrf": csrf,
        "user_id": "viewer",
        "display_name": "<script>alert(1)</script>",
        "email": "viewer@example.org",
    }
    denied = request(webadmin, "/users", method="POST", cookie=cookie,
                     form={**new_user, "csrf": "bad"})
    assert b"CSRF check failed" in denied["body"]
    created = request(webadmin, "/users", method="POST", cookie=cookie, form=new_user)
    assert created["status"] == "200 OK"
    assert b"&lt;script&gt;alert(1)&lt;/script&gt;" in created["body"]
    assert b"<script>alert(1)</script>" not in created["body"]


def test_json_rpc_login_bearer_auth_errors_and_legacy_auth(webadmin):
    response, result = rpc(
        webadmin,
        "mmfdb.security.auth.login",
        {"user_id": "admin", "password": "Admin123!"},
    )
    assert response["status"] == "200 OK"
    token = result["result"]["token"]

    _, status = rpc(webadmin, "mmfdb.status", bearer=token)
    assert status["result"]["user_count"] >= 2
    # The status envelope advertises the SQL backend and object-store backend so
    # a remote client can display the connection's database type.
    assert status["result"]["database_dialect"] == "sqlite"
    assert status["result"]["database_location"]
    assert status["result"]["object_store_backend"] == "local"

    _, legacy = rpc(webadmin, "mmfdb.status", {"auth": {"token": token}})
    assert legacy["result"]["schema_version"]

    _, unauthorized = rpc(webadmin, "mmfdb.status", bearer="invalid")
    assert unauthorized["error"]["code"] == -32001
    _, missing = rpc(webadmin, "mmfdb.does-not-exist", bearer=token)
    assert missing["error"]["code"] == -32601


def test_web_resources_load_from_package_namespace():
    app = WebAdminApp(csrf_secret=b"test-secret")
    assert "<!doctype html>" in app.base_template
    assert "<!doctype html>" in app.login_template


def test_json_rpc_cap_stays_small_when_binary_uploads_are_supported():
    assert MAX_REQUEST_BYTES == 1024 * 1024
    assert MAX_REQUEST_BYTES < MAX_OBJECT_UPLOAD_BYTES


def test_binary_object_upload_download_streaming_and_acl(webadmin):
    _, token = login(webadmin)
    payload = (b"\x00MMFDB-binary\xff" * 100_000) + b"tail"
    upload = request(
        webadmin,
        "/objects",
        method="POST",
        bearer=token,
        body=payload,
        content_type="application/octet-stream",
        headers={"X-MMFDB-Filename": "trace.ptu"},
    )
    assert upload["status"] == "201 Created"
    result = json.loads(upload["body"])
    object_uuid = result["object"]["object_uuid"]
    assert result["object"]["size_bytes"] == len(payload)

    denied = request(webadmin, f"/objects/{object_uuid}")
    assert denied["status"] == "401 Unauthorized"
    download = request(webadmin, f"/objects/{object_uuid}", bearer=token)
    assert download["status"] == "200 OK"
    assert download["body"] == payload
    assert len(download["chunks"]) > 1
    assert header(download, "Content-Length") == str(len(payload))
    assert "trace.ptu" in header(download, "Content-Disposition")


def test_rpc_bytes_and_streamed_paths_share_deduplication_and_acl_contract(webadmin):
    _, token = login(webadmin)
    payload = b"same-content-through-two-transports"
    _, rpc_result = rpc(
        webadmin,
        "mmfdb.objects.put_bytes",
        {
            "data": base64.b64encode(payload).decode("ascii"),
            "filename": "rpc.bin",
        },
        bearer=token,
    )
    streamed = request(
        webadmin,
        "/objects",
        method="POST",
        bearer=token,
        body=payload,
        headers={"X-MMFDB-Filename": "stream.bin"},
    )
    stream_object = json.loads(streamed["body"])["object"]

    assert stream_object["object_uuid"] == rpc_result["result"]["object"]["object_uuid"]
    assert stream_object["deduplicated"] is True
    assert request(
        webadmin, f'/objects/{stream_object["object_uuid"]}', bearer=token
    )["body"] == payload


def test_binary_upload_rejects_oversize_before_reading(webadmin):
    _, token = login(webadmin)
    response = request(
        webadmin,
        "/objects",
        method="POST",
        bearer=token,
        body=b"not-read",
        content_length=MAX_OBJECT_UPLOAD_BYTES + 1,
    )
    assert response["status"] == "413 Content Too Large"


def test_browser_object_admin_can_upload_download_and_delete(webadmin):
    cookie, _ = login(webadmin)
    page = request(webadmin, "/object-admin", cookie=cookie)
    assert page["status"] == "200 OK"
    assert b'type="file"' in page["body"]
    csrf = re.search(rb'name="csrf" value="([a-f0-9]+)"', page["body"]).group(1).decode()

    upload = request(
        webadmin,
        "/objects",
        method="POST",
        cookie=cookie,
        csrf=csrf,
        body=b"browser-object",
        headers={"X-MMFDB-Filename": "browser.bin"},
    )
    assert upload["status"] == "201 Created"
    object_uuid = json.loads(upload["body"])["object"]["object_uuid"]
    listing = request(webadmin, "/object-admin", cookie=cookie)
    assert object_uuid.encode() in listing["body"]
    assert f'/objects/{object_uuid}'.encode() in listing["body"]

    downloaded = request(webadmin, f"/objects/{object_uuid}", cookie=cookie)
    assert downloaded["body"] == b"browser-object"
    deleted = request(
        webadmin,
        "/object-admin",
        method="POST",
        cookie=cookie,
        form={"csrf": csrf, "action": "delete", "object_uuid": object_uuid},
    )
    assert deleted["status"] == "302 Found"
    assert object_uuid.encode() not in request(
        webadmin, "/object-admin", cookie=cookie
    )["body"]


def test_readiness_fails_when_local_object_store_is_not_writable(webadmin, tmp_path):
    invalid_root = tmp_path / "not-a-directory"
    invalid_root.write_text("file", encoding="utf-8")
    configure_runtime(object_store_root=invalid_root)

    response = request(webadmin, "/health/ready")

    assert response["status"] == "503 Service Unavailable"
    payload = json.loads(response["body"])
    assert payload["ready"] is False
    assert payload["checks"]["database"]["ok"] is True
    assert payload["checks"]["object_store"]["ok"] is False


@pytest.mark.parametrize(
    ("slug", "record_id", "create_form", "updated_value"),
    [
        ("samples", "sample-web", {"description": "first sample"}, "updated sample"),
        ("experiments", "experiment-web", {"status": "planned"}, "complete"),
        ("devices", "device-web", {"name": "Microscope", "device_type": "confocal"}, "Updated scope"),
        ("entities", "entity-web", {"common_name": "Protein A", "type": "polymer"}, "Protein B"),
    ],
)
def test_core_entity_web_crud_is_csrf_protected_and_editable(
    webadmin, slug, record_id, create_form, updated_value
):
    cookie, _ = login(webadmin)
    page = request(webadmin, f"/entities/{slug}", cookie=cookie)
    assert b"Create" in page["body"]
    csrf = re.search(rb'name="csrf" value="([a-f0-9]+)"', page["body"]).group(1).decode()
    id_field = {"samples": "sample_id", "experiments": "experiment_id",
                "devices": "device_id", "entities": "entity_id"}[slug]

    rejected = request(
        webadmin, f"/entities/{slug}", method="POST", cookie=cookie,
        form={id_field: record_id, **create_form, "csrf": "wrong", "action": "save"},
    )
    assert rejected["status"] == "403 Forbidden"

    created = request(
        webadmin, f"/entities/{slug}", method="POST", cookie=cookie,
        form={id_field: record_id, **create_form, "csrf": csrf, "action": "save"},
    )
    assert created["status"] == "302 Found"
    edit = request(webadmin, f"/entities/{slug}/{record_id}", cookie=cookie)
    assert edit["status"] == "200 OK"
    assert record_id.encode() in edit["body"]

    update_field = {"samples": "description", "experiments": "status",
                    "devices": "name", "entities": "common_name"}[slug]
    updated = request(
        webadmin, f"/entities/{slug}", method="POST", cookie=cookie,
        form={id_field: record_id, **create_form, update_field: updated_value,
              "csrf": csrf, "action": "save"},
    )
    assert updated["status"] == "302 Found"
    assert updated_value.encode() in request(
        webadmin, f"/entities/{slug}/{record_id}", cookie=cookie
    )["body"]

    deleted = request(
        webadmin, f"/entities/{slug}", method="POST", cookie=cookie,
        form={id_field: record_id, "csrf": csrf, "action": "delete"},
    )
    assert deleted["status"] == "302 Found"
    assert record_id.encode() not in request(
        webadmin, f"/entities/{slug}", cookie=cookie
    )["body"]


def test_web_edit_preserves_nested_sample_data_not_present_in_simple_form(webadmin):
    cookie, token = login(webadmin)
    _, saved = rpc(
        webadmin,
        "mmfdb.samples.save",
        {
            "sample": {
                "sample_id": "nested-sample",
                "description": "before",
                "key_values": [{"key": "temperature", "value": "298 K"}],
            }
        },
        bearer=token,
    )
    assert saved["result"]["sample"]["key_values"]
    page = request(webadmin, "/entities/samples/nested-sample", cookie=cookie)
    csrf = re.search(rb'name="csrf" value="([a-f0-9]+)"', page["body"]).group(1).decode()

    response = request(
        webadmin,
        "/entities/samples",
        method="POST",
        cookie=cookie,
        form={
            "csrf": csrf,
            "action": "save",
            "sample_id": "nested-sample",
            "description": "after",
        },
    )

    assert response["status"] == "302 Found"
    _, loaded = rpc(
        webadmin,
        "mmfdb.samples.get",
        {"sample_id": "nested-sample"},
        bearer=token,
    )
    assert loaded["result"]["sample"]["description"] == "after"
    assert loaded["result"]["sample"]["key_values"] == [
        {"key": "temperature", "value": "298 K", "details": None}
    ]
