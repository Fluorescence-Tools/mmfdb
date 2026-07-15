# MMFDB web administration and HTTP API

MMFDB includes an additive, standalone web surface. It uses the same
`mmfdb.admin.backend` handlers as an embedded desktop administrator, so
authorization and mutations have one backend contract rather than two
implementations.

## Run

The deployment CLI starts this application through `mmfdb.webadmin.serve`. For
development it can also be run directly:

```console
MMFDB_ADMIN_PASSWORD='Replace-this1!' \
  python -m mmfdb.webadmin --config mmfdb.example.yaml
```

The module command intentionally routes through `mmfdb serve`: it validates the
YAML, initializes storage, and performs the one-shot administrator bootstrap
before binding HTTP. `--host` and `--port` remain available as explicit
overrides after `--config`.

Open `http://127.0.0.1:8080/login`. Only an MMFDB administrator can enter the
browser UI. The overview, users, scientific/LIMS entities, projects, data,
provenance, audit, sessions, and the full registered RPC catalogue are
available from its navigation. Samples, experiments, devices, and molecular
entities have ordinary CSRF-protected create/edit/delete forms. The Objects
page uploads, lists, downloads, and deletes stored files without requiring the
RPC explorer.

## JSON-RPC contract

Send JSON-RPC 2.0 requests to `POST /rpc`:

```json
{"jsonrpc":"2.0","id":1,"method":"mmfdb.security.auth.login","params":{"user_id":"admin","password":"..."}}
```

Use the returned session token as `Authorization: Bearer <token>` on subsequent
calls. Legacy `params.auth.token` remains accepted because it is the native
backend service contract. JSON-RPC errors use `-32600` (request), `-32601`
(method), `-32602` (parameters), `-32603` (internal), and `-32001`
(authentication/authorization).

JSON-RPC requests are capped at 1 MiB. Raw object bodies deliberately use a
separate transport so base64 expansion cannot turn the threaded RPC process
into an approximately 90 MiB per-request memory boundary.

## Binary objects

Upload a raw body of at most 64 MiB to `POST /objects`. Supply the session as a
Bearer token, the original filename as a percent-encoded `X-MMFDB-Filename`
header, and its MIME type as `Content-Type`. Optional metadata is a base64url-
encoded JSON object in `X-MMFDB-Metadata`. `Content-Length` is required and is
rejected before the body is read when it exceeds the limit.

Download the verified bytes with `GET /objects/<object-uuid>`. Both endpoints
apply the same session authentication, object ACL, content-addressed
deduplication, and reference ownership contract as `mmfdb.objects.*`. A remote
client's `put_object` / `get_object` methods select this transport automatically;
embedded clients retain their existing in-process behavior.

## Health probes

`GET /health/live` checks only that the HTTP process can answer. `GET /healthz`
and its explicit alias `GET /health/ready` open the configured database, issue
a query, read its schema version, and perform a create/write/fsync/delete probe
in the configured local object-store root. They return HTTP 503 when either
dependency is unavailable. These endpoints are intentionally unauthenticated
and expose only check categories and exception types on failure.

## Production boundary

The bundled server is concurrent and has no optional framework dependency.
Deploy it behind a reverse proxy that terminates TLS, applies connection and
request timeouts, and limits accepted network origins. Set
`MMFDB_WEB_SECURE_COOKIE=true` whenever the browser interface is exposed over
HTTPS. Session cookies are HTTP-only and same-site; browser mutations also
require a per-session CSRF token. Configure the container orchestrator's
readiness probe against `/healthz` and its liveness probe against
`/health/live`.

An embedding application may reuse the same YAML without receiving server
credentials. It reads only the `server` and `client` hints through
`mmfdb.load_client_config`; missing
environment variables under `admin`, LDAP, database, or object storage do not
need to be present in the desktop process. Set `client.mode: embedded` to retain
the in-process database, or `client.mode: remote` with `client.base_url` for the
standalone service. Non-loopback remote URLs require HTTPS. An isolated
development network may opt in to plaintext explicitly with
`client.allow_insecure_http: true`; URL userinfo, query strings, and fragments
are always rejected.
