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

## Reference client (tested, standard library only)

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
A fuller client (`MMFDBClient`) with the same wire contract plus streaming and
settings integration currently lives in a downstream consumer application;
moving a standalone client into the `mmfdb` package is tracked as **INC-09**.
Until then, copy the ~50-line reference above.
```
