"""A minimal MMFDB RPC client — the reference an agent copies to build its own.

MMFDB speaks **JSON-RPC 2.0 over HTTP**. Any language with an HTTP client can
talk to it; this file implements a complete client in ~50 lines using only the
Python standard library — no ChiSurf, no `requests`. The wire contract it relies
on is documented in ``docs/rpc-clients.md`` and exercised by
``tests/test_min_client.py``.

The three surfaces:

* ``POST /rpc``            — JSON-RPC calls (all metadata / provenance methods)
* ``POST /objects``       — upload raw bytes, returns an object UUID
* ``GET  /objects/<uuid>``— download raw bytes

Authentication is a bearer token from ``mmfdb.security.auth.login``, sent in the
``Authorization`` header on every subsequent request.
"""

from __future__ import annotations

import json
import pathlib
import urllib.error
import urllib.request


class MmfdbClient:
    """Standalone MMFDB JSON-RPC client (standard library only)."""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.token: str | None = None

    def _post_rpc(self, method: str, params: dict) -> dict:
        """POST one JSON-RPC call; return the handler result or raise."""
        body = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        ).encode()
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(
            f"{self.base_url}/rpc", data=body, headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(request) as response:
                payload = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            payload = json.loads(exc.read())
        if "error" in payload:  # JSON-RPC 2.0 error member
            raise RuntimeError(payload["error"])
        return payload["result"]  # the handler dict, e.g. {"ok": True, ...}

    def login(self, user_id: str, password: str = "") -> None:
        """Authenticate and store the bearer token for later calls."""
        result = self._post_rpc(
            "mmfdb.security.auth.login", {"user_id": user_id, "password": password}
        )
        self.token = result["token"]

    def call(self, method: str, params: dict | None = None) -> dict:
        """Call any JSON-RPC method (e.g. ``mmfdb.v1.artifacts.register``)."""
        return self._post_rpc(method, params or {})

    def put_object(self, path: str) -> str:
        """Upload a file's bytes to the object store; return its object UUID."""
        data = pathlib.Path(path).read_bytes()
        request = urllib.request.Request(
            f"{self.base_url}/objects",
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/octet-stream",
                "X-MMFDB-Filename": pathlib.Path(path).name,
                "Authorization": f"Bearer {self.token}",
            },
        )
        with urllib.request.urlopen(request) as response:
            return json.loads(response.read())["object"]["object_uuid"]

    def get_object(self, object_uuid: str) -> bytes:
        """Download raw bytes for an object UUID."""
        request = urllib.request.Request(
            f"{self.base_url}/objects/{object_uuid}",
            headers={"Authorization": f"Bearer {self.token}"},
        )
        with urllib.request.urlopen(request) as response:
            return response.read()
