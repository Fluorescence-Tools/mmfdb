"""Test the documented minimal MMFDB RPC client (docs/rpc-clients.md).

Keeps the reference client an agent copies honest: it is exercised against a
live, chisurf-free MMFDB server for login, a JSON-RPC call, an object-store
round-trip, and error propagation.
"""

from __future__ import annotations

import importlib.util
import socket
import threading
from pathlib import Path
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

import pytest

pytest.importorskip("mmfdb")

_REPO = Path(__file__).resolve().parents[1]
_CLIENT_PATH = _REPO / "examples" / "external_tools" / "mmfdb_min_client.py"


def _load_client_class():
    """Import ``MmfdbClient`` from the standalone example file by path."""
    spec = importlib.util.spec_from_file_location("mmfdb_min_client", _CLIENT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.MmfdbClient


class _QuietHandler(WSGIRequestHandler):
    def log_message(self, *args: object) -> None:
        return


class _ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True


@pytest.fixture()
def server(tmp_path):
    """Serve a fresh chisurf-free MMFDB instance and yield (base_url, client_cls)."""
    from mmfdb.config import configure_runtime, reset_runtime_config
    from mmfdb.repository import MFDatabase
    from mmfdb.security.bootstrap import bootstrap_local_admin
    from mmfdb.webadmin import create_app

    configure_runtime(database_path=tmp_path / "db.sqlite", settings_dir=tmp_path)
    with MFDatabase(str(tmp_path / "db.sqlite")) as db:
        bootstrap_local_admin(db.conn, user_id="admin", password="Admin123!", commit=True)

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    httpd = make_server(
        "127.0.0.1", port, create_app(),
        server_class=_ThreadingWSGIServer, handler_class=_QuietHandler,
    )
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}", _load_client_class()
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()
        reset_runtime_config()


def test_login_and_object_round_trip(server, tmp_path):
    """The client logs in and round-trips bytes through the object store."""
    base_url, MmfdbClient = server
    client = MmfdbClient(base_url)
    client.login("admin", "Admin123!")
    assert client.token

    blob = tmp_path / "raw.bin"
    blob.write_bytes(b"single-molecule photons" * 1000)
    uuid = client.put_object(str(blob))
    assert uuid
    assert client.get_object(uuid) == blob.read_bytes()


def test_json_rpc_call_and_error(server):
    """A JSON-RPC call returns the handler result; unknown methods raise."""
    base_url, MmfdbClient = server
    client = MmfdbClient(base_url)
    client.login("admin", "Admin123!")

    status = client.call("mmfdb.status")
    assert isinstance(status, dict)

    with pytest.raises(RuntimeError):
        client.call("this.method.does.not.exist")
