"""Standard-library HTTP JSON-RPC client for a remote MMFDB server.

This is the packaged, framework-free transport an external tool imports instead
of copying the reference client in ``examples/external_tools/``.  It speaks the
wire contract documented in ``docs/rpc-clients.md``: JSON-RPC over ``POST /rpc``
with a bearer token, and raw object bodies over ``POST``/``GET /objects``.

The module deliberately depends on nothing beyond the standard library so that
importing it costs nothing and pulls in neither the database nor the admin host.
Embedding applications layer their own settings, credential storage and
alternative transports on top of it.
"""

from __future__ import annotations

import base64
import http.client
import io
import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from ipaddress import ip_address
from typing import Any

__all__ = ["HttpJsonRpcClient", "validate_base_url"]

#: Largest metadata header accepted by the object endpoint, in bytes.
MAX_OBJECT_METADATA_BYTES = 16 * 1024

#: Largest JSON body read back from the object endpoint, in bytes.
MAX_OBJECT_RESPONSE_BYTES = 1024 * 1024

_CHUNK_BYTES = 64 * 1024


def validate_base_url(base_url: str, *, allow_insecure_http: bool = False) -> str:
    """Validate and normalize a credential-free MMFDB HTTP endpoint.

    Plain HTTP is refused for a non-loopback host unless the caller explicitly
    opts in, so a misconfigured deployment cannot ship bearer tokens in clear
    text without saying so.
    """
    if not isinstance(allow_insecure_http, bool):
        raise ValueError("mmfdb.client.allow_insecure_http must be true or false")
    normalized = base_url.rstrip("/")
    parsed = urllib.parse.urlsplit(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("mmfdb.client.base_url must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("mmfdb.client.base_url must not contain user credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("mmfdb.client.base_url must not contain a query or fragment")
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError("mmfdb.client.base_url contains an invalid port") from exc
    hostname = parsed.hostname.lower()
    is_loopback = hostname == "localhost"
    try:
        is_loopback = is_loopback or ip_address(hostname).is_loopback
    except ValueError:
        pass
    if parsed.scheme == "http" and not is_loopback and not allow_insecure_http:
        raise ValueError(
            "mmfdb.client.base_url must use HTTPS for a non-loopback host; set "
            "allow_insecure_http only for an isolated development network"
        )
    return normalized


def _max_object_bytes() -> int:
    """Return the server's configured object-size ceiling.

    Imported lazily: the limit lives with the endpoint that enforces it, and a
    client that never touches the object store should not pay for the import.
    """
    from mmfdb.admin.backend.services import MAX_OBJECT_UPLOAD_BYTES

    return MAX_OBJECT_UPLOAD_BYTES


class HttpJsonRpcClient:
    """Small synchronous JSON-RPC transport for standalone MMFDB."""

    def __init__(
        self,
        base_url: str,
        timeout_ms: int,
        *,
        allow_insecure_http: bool = False,
    ) -> None:
        normalized = validate_base_url(
            base_url,
            allow_insecure_http=allow_insecure_http,
        )
        self._rpc_url = f"{normalized}/rpc"
        self._base = urllib.parse.urlsplit(normalized)
        if self._base.scheme not in {"http", "https"} or not self._base.hostname:
            raise ValueError("MMFDB base URL must be an absolute HTTP(S) URL")
        self._object_path = f"{self._base.path.rstrip('/')}/objects"
        self._timeout = timeout_ms / 1000.0
        self._request_id = 0

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send one JSON-RPC request; transport failures come back as a payload."""
        self._request_id += 1
        wire_params = dict(params or {})
        auth = wire_params.pop("auth", None)
        headers = {"Content-Type": "application/json"}
        if isinstance(auth, Mapping) and auth.get("token"):
            headers["Authorization"] = f"Bearer {auth['token']}"
        payload = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": self._request_id,
                "method": method,
                "params": wire_params,
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            self._rpc_url,
            data=payload,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                return json.loads(exc.read().decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return {"ok": False, "error": f"HTTP {exc.code}: {exc.reason}"}
        except urllib.error.URLError as exc:
            return {"ok": False, "error": f"MMFDB server unavailable: {exc.reason}"}

    def upload_object(
        self,
        stream: Any,
        *,
        length: int,
        filename: str,
        mime_type: str | None,
        metadata: dict[str, Any] | None,
        token: str | None,
    ) -> dict[str, Any]:
        """Stream one raw object body to the bounded HTTP object endpoint."""
        if not token:
            raise RuntimeError("Login required for MMFDB object upload")
        if length < 0 or length > _max_object_bytes():
            raise ValueError("MMFDB object upload exceeds the configured size limit")
        metadata_header = None
        if metadata is not None:
            metadata_header = base64.urlsafe_b64encode(
                json.dumps(metadata, separators=(",", ":")).encode("utf-8")
            ).decode("ascii").rstrip("=")
            if len(metadata_header) > MAX_OBJECT_METADATA_BYTES:
                raise ValueError("MMFDB object metadata header exceeds 16 KiB")
        connection = self._connection()
        try:
            connection.putrequest("POST", self._object_path)
            connection.putheader("Content-Length", str(length))
            connection.putheader("Content-Type", mime_type or "application/octet-stream")
            connection.putheader("X-MMFDB-Filename", urllib.parse.quote(filename, safe=""))
            if metadata_header is not None:
                connection.putheader("X-MMFDB-Metadata", metadata_header)
            if token:
                connection.putheader("Authorization", f"Bearer {token}")
            connection.endheaders()
            remaining = length
            while remaining:
                chunk = stream.read(min(_CHUNK_BYTES, remaining))
                if not chunk:
                    raise RuntimeError("Object source ended before its declared length")
                connection.send(chunk)
                remaining -= len(chunk)
            response = connection.getresponse()
            payload = response.read(MAX_OBJECT_RESPONSE_BYTES + 1)
            if len(payload) > MAX_OBJECT_RESPONSE_BYTES:
                raise RuntimeError("MMFDB object response exceeded 1 MiB")
            try:
                result = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RuntimeError(
                    f"Invalid MMFDB object response (HTTP {response.status})"
                ) from exc
            if response.status >= 400 or not result.get("ok", False):
                detail = result.get("error") or f"HTTP {response.status}: {response.reason}"
                raise RuntimeError(str(detail))
            return result
        finally:
            connection.close()

    def download_object(self, object_uuid: str, *, token: str | None) -> bytes:
        """Download one raw object in bounded chunks."""
        if not token:
            raise RuntimeError("Login required for MMFDB object download")
        max_bytes = _max_object_bytes()
        connection = self._connection()
        path = f"{self._object_path}/{urllib.parse.quote(object_uuid, safe='')}"
        try:
            connection.putrequest("GET", path)
            if token:
                connection.putheader("Authorization", f"Bearer {token}")
            connection.endheaders()
            response = connection.getresponse()
            if response.status >= 400:
                payload = response.read(MAX_OBJECT_RESPONSE_BYTES)
                try:
                    detail = json.loads(payload.decode("utf-8")).get("error")
                except (UnicodeDecodeError, json.JSONDecodeError):
                    detail = None
                raise RuntimeError(str(detail or f"HTTP {response.status}: {response.reason}"))
            declared = response.getheader("Content-Length")
            if declared is None:
                raise RuntimeError("MMFDB object download omitted Content-Length")
            length = int(declared)
            if length < 0 or length > max_bytes:
                raise RuntimeError("MMFDB object download exceeds the 64 MiB limit")
            target = io.BytesIO()
            remaining = length
            while remaining:
                chunk = response.read(min(_CHUNK_BYTES, remaining))
                if not chunk:
                    raise RuntimeError("MMFDB object download ended early")
                target.write(chunk)
                remaining -= len(chunk)
            return target.getvalue()
        finally:
            connection.close()

    def _connection(self) -> http.client.HTTPConnection:
        """Open one connection matching the endpoint's scheme."""
        connection_type = (
            http.client.HTTPSConnection
            if self._base.scheme == "https"
            else http.client.HTTPConnection
        )
        return connection_type(
            self._base.hostname,
            port=self._base.port,
            timeout=self._timeout,
        )
