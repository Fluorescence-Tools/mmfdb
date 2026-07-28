# Implementing an MMFDB client

MMFDB speaks **JSON-RPC 2.0 over HTTP**. Any language with an HTTP client can be
a client — there is no MMFDB SDK to depend on. This page is the *complete* wire
contract; implement against it, not against any particular client. The contract
is intentionally small and stable, so a client stays ~50 lines.

A working reference implementation (Python standard library only, no framework) is
`examples/external_tools/mmfdb_min_client.py`, kept honest by
`tests/test_min_client.py`.

## The three surfaces

| Method + path | Purpose |
|---|---|
| `POST /rpc` | all metadata / provenance calls (JSON-RPC 2.0) |
| `POST /objects` | upload raw bytes → returns an object UUID |
| `GET /objects/{uuid}` | download raw bytes |

`GET /healthz` (no auth) reports readiness.

## JSON-RPC calls (`POST /rpc`)

**Request** — one JSON object:

```json
{"jsonrpc": "2.0", "id": 1, "method": "mmfdb.v1.artifacts.list", "params": {}}
```

**Success** — the handler's return value is in `result` (usually a dict with an
`"ok": true` field plus payload):

```json
{"jsonrpc": "2.0", "id": 1, "result": {"ok": true, "artifacts": []}}
```

**Error** — a JSON-RPC `error` member; raise on it:

```json
{"jsonrpc": "2.0", "id": 1, "error": {"code": -32601, "message": "Method not found"}}
```

Codes are standard JSON-RPC: `-32700` parse, `-32600` invalid request, `-32601`
method not found, `-32602` invalid params (plus `-32002` CSRF for browser posts).
`params` are exactly the handler's keyword arguments — see the function
signatures in `src/mmfdb/api.py`.

## Authentication

1. Call `mmfdb.security.auth.login` with `{"user_id": ..., "password": ...}`.
2. Read the token from `result["token"]`.
3. Send `Authorization: Bearer <token>` on every subsequent request (RPC and
   object). Object endpoints require it.

## The object store

- **Upload:** `POST /objects` with the raw bytes as the body,
  `Content-Type: application/octet-stream`, an `X-MMFDB-Filename` header, and the
  bearer token. The response is `{"ok": true, "object": {"object_uuid": ...}}`
  (max 64 MiB). Optional metadata rides in a base64url `X-MMFDB-Metadata` header.
- **Download:** `GET /objects/{uuid}` with the bearer token → the raw bytes.

## Packaged client (`mmfdb.client`)

If you can import `mmfdb`, do not write a transport at all — the package ships
one. `mmfdb.client` depends on nothing beyond the standard library, so importing
it costs neither the database nor the admin host:

```python
from mmfdb.client import HttpJsonRpcClient

client = HttpJsonRpcClient("https://mmfdb.mylab.org", timeout_ms=5000)
login = client.call("mmfdb.security.auth.login", {"user_id": "me", "password": "secret"})
token = login["result"]["token"]

client.call("mmfdb.status", {"auth": {"token": token}})         # bearer header
with open("measurement.spc", "rb") as source:                   # streamed, bounded
    stored = client.upload_object(
        source, length=size, filename="measurement.spc",
        mime_type=None, metadata={"kind": "raw"}, token=token,
    )
raw = client.download_object(stored["object"]["object_uuid"], token=token)
```

`validate_base_url()` decides the endpoint policy in one place: the URL must be
absolute, credential-free and without a query or fragment, and plain HTTP is
refused for a non-loopback host unless `allow_insecure_http=True` says so
explicitly. Object bodies are streamed in 64 KiB chunks and bounded on both
sides, so neither a hostile response nor a short source stream is silently
truncated.

## Reference client (copy-paste, no `mmfdb` install)

```{literalinclude} ../examples/external_tools/mmfdb_min_client.py
:language: python
:pyobject: MmfdbClient
```

Usage:

```python
client = MmfdbClient("http://mmfdb.mylab.org")
client.login("me", "secret")
uuid = client.put_object("measurement.spc")                     # store raw bytes
client.call("mmfdb.v1.artifacts.register",                      # register metadata
            {"artifact_id": "raw/1", "artifact_kind": "raw_measurement",
             "checksum": "…64 hex…"})
```

## Finding method names

Methods are grouped by namespace; the ones a client needs most:

| Namespace | What it does |
|---|---|
| `mmfdb.security.auth.*` | `login`, `logout`, `me` |
| `mmfdb.v1.artifacts.*` | `register`, `get`, `list` |
| `mmfdb.v1.operations.*` | `record`, `record_with_artifacts`, `link_artifact`, `get`, `list` |
| `provenance.*` | `dependencies.upstream` / `downstream`, `edges.list`, `graph.export` |
| `raw_data.*`, `processed_data.*`, `analysis.run.*` | measurement records |
| `mmfdb.status` | server / database info |

The authoritative registry is
`src/mmfdb/admin/backend/services.py` (and `measurement_services.py`);
a running server also lists every method at `/rpc-explorer`.

## Two ways to talk to MMFDB

- **HTTP client (this page)** — for a remote or shared server; portable, no
  framework. This is what an external tool or another language uses.
- **Embedded** — in the same process, `mmfdb.repository.MFDatabase` methods work
  directly against a local SQLite file with no server (see
  `examples/mmfdb_08_standalone_no_lockin.ipynb`).

```{note}
The HTTP transport now ships in the package as `mmfdb.client` (**INC-09**); the
copy-paste reference above stays for tools that cannot install `mmfdb` at all.
What still lives in a downstream consumer application is the *ergonomic* layer
on top — the ~90 named method wrappers, settings/credential-store integration
and the alternative in-process and message-queue transports.
```
