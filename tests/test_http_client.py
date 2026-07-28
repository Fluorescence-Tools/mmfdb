"""The packaged HTTP JSON-RPC client (INC-09).

Exercised against a throwaway ``http.server`` rather than a live MMFDB server:
the point of ``mmfdb.client`` is that it is pure transport, so these tests pin
the wire contract and the safety bounds without booting the database.
"""

from __future__ import annotations

import io
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from mmfdb.client import HttpJsonRpcClient, validate_base_url


class _Handler(BaseHTTPRequestHandler):
    """Minimal stand-in for the ``/rpc`` and ``/objects`` endpoints."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *args: object) -> None:
        return

    def _respond(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler naming
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        if self.path == "/rpc":
            request = json.loads(body.decode("utf-8"))
            self.server.rpc_requests.append((request, dict(self.headers)))
            if request["method"] == "boom":
                self._respond(500, {"ok": False, "error": "exploded"})
                return
            self._respond(200, {"jsonrpc": "2.0", "id": request["id"], "result": {"ok": True}})
            return
        if self.path == "/objects":
            self.server.uploads.append((body, dict(self.headers)))
            self._respond(200, {"ok": True, "object": {"object_uuid": "obj-1"}})
            return
        self._respond(404, {"ok": False, "error": "no such path"})

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler naming
        if self.path == "/objects/obj-1":
            payload = self.server.blob
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self._respond(404, {"ok": False, "error": "no such object"})


@pytest.fixture()
def server():
    """Run the stand-in endpoint on a loopback port for one test."""
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    httpd.rpc_requests = []
    httpd.uploads = []
    httpd.blob = b"payload-bytes"
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _client(server, **kwargs) -> HttpJsonRpcClient:
    host, port = server.server_address[:2]
    return HttpJsonRpcClient(f"http://{host}:{port}", 5000, **kwargs)


class TestValidateBaseUrl:
    """``validate_base_url`` is the one place the endpoint policy is decided."""

    def test_https_is_normalized(self) -> None:
        assert validate_base_url("https://mmfdb.example.test/api/") == (
            "https://mmfdb.example.test/api"
        )

    def test_plain_http_allowed_on_loopback(self) -> None:
        assert validate_base_url("http://127.0.0.1:8080") == "http://127.0.0.1:8080"
        assert validate_base_url("http://localhost:8080") == "http://localhost:8080"

    def test_plain_http_refused_off_loopback(self) -> None:
        with pytest.raises(ValueError, match="HTTPS"):
            validate_base_url("http://mmfdb.example.test")

    def test_plain_http_off_loopback_needs_an_explicit_opt_in(self) -> None:
        assert (
            validate_base_url("http://mmfdb.example.test", allow_insecure_http=True)
            == "http://mmfdb.example.test"
        )

    @pytest.mark.parametrize(
        ("url", "message"),
        [
            ("ftp://mmfdb.example.test", "absolute HTTP"),
            ("mmfdb.example.test", "absolute HTTP"),
            ("https://user:pw@mmfdb.example.test", "credentials"),
            ("https://mmfdb.example.test?a=1", "query or fragment"),
            ("https://mmfdb.example.test#frag", "query or fragment"),
            ("https://mmfdb.example.test:notaport", "invalid port"),
        ],
    )
    def test_rejected_urls(self, url: str, message: str) -> None:
        with pytest.raises(ValueError, match=message):
            validate_base_url(url)

    def test_opt_in_flag_must_be_boolean(self) -> None:
        with pytest.raises(ValueError, match="true or false"):
            validate_base_url("https://mmfdb.example.test", allow_insecure_http="yes")


class TestCall:
    """JSON-RPC requests and the failure modes a caller has to handle."""

    def test_round_trip_and_incrementing_ids(self, server) -> None:
        client = _client(server)
        assert client.call("mmfdb.status")["result"] == {"ok": True}
        client.call("mmfdb.status")
        ids = [request["id"] for request, _headers in server.rpc_requests]
        assert ids == [1, 2]

    def test_auth_token_travels_as_a_bearer_header_not_a_parameter(self, server) -> None:
        client = _client(server)
        client.call("mmfdb.status", {"auth": {"token": "t0ken"}, "scope": "all"})
        request, headers = server.rpc_requests[-1]
        assert headers["Authorization"] == "Bearer t0ken"
        assert request["params"] == {"scope": "all"}

    def test_error_body_is_returned_not_raised(self, server) -> None:
        assert _client(server).call("boom") == {"ok": False, "error": "exploded"}

    def test_unreachable_server_reports_unavailable(self) -> None:
        client = HttpJsonRpcClient("http://127.0.0.1:1", 500)
        result = client.call("mmfdb.status")
        assert result["ok"] is False
        assert "unavailable" in result["error"]


class TestObjectStore:
    """Raw object bodies, and the bounds that keep them from being unbounded."""

    def test_upload_round_trip_sends_filename_and_metadata(self, server) -> None:
        client = _client(server)
        result = client.upload_object(
            io.BytesIO(b"raw-bytes"),
            length=9,
            filename="measurement 1.spc",
            mime_type=None,
            metadata={"kind": "raw"},
            token="t0ken",
        )
        assert result["object"]["object_uuid"] == "obj-1"
        body, headers = server.uploads[-1]
        assert body == b"raw-bytes"
        assert headers["X-MMFDB-Filename"] == "measurement%201.spc"
        assert headers["Authorization"] == "Bearer t0ken"
        assert headers["X-MMFDB-Metadata"]

    def test_download_round_trip(self, server) -> None:
        assert _client(server).download_object("obj-1", token="t0ken") == b"payload-bytes"

    def test_download_of_a_missing_object_raises_the_server_message(self, server) -> None:
        with pytest.raises(RuntimeError, match="no such object"):
            _client(server).download_object("obj-2", token="t0ken")

    def test_object_access_requires_a_token(self, server) -> None:
        client = _client(server)
        with pytest.raises(RuntimeError, match="Login required"):
            client.upload_object(
                io.BytesIO(b""),
                length=0,
                filename="x",
                mime_type=None,
                metadata=None,
                token=None,
            )
        with pytest.raises(RuntimeError, match="Login required"):
            client.download_object("obj-1", token=None)

    def test_oversized_upload_is_refused_before_a_connection_is_opened(self, server) -> None:
        from mmfdb.admin.backend.services import MAX_OBJECT_UPLOAD_BYTES

        with pytest.raises(ValueError, match="size limit"):
            _client(server).upload_object(
                io.BytesIO(b""),
                length=MAX_OBJECT_UPLOAD_BYTES + 1,
                filename="x",
                mime_type=None,
                metadata=None,
                token="t0ken",
            )
        assert server.uploads == []

    def test_a_short_source_stream_is_an_error_not_a_truncated_object(self, server) -> None:
        with pytest.raises(RuntimeError, match="ended before its declared length"):
            _client(server).upload_object(
                io.BytesIO(b"three"),
                length=99,
                filename="x",
                mime_type=None,
                metadata=None,
                token="t0ken",
            )
