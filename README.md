# MMFDB

<p align="center">
  <img src="assets/mmfdb-overview.svg" width="860"
       alt="MMFDB pipeline: measure → store → analyse → deposit, with a provenance graph linking every result back to its raw data and the exact tool that produced it.">
</p>

MMFDB is a **multimodal fluorescence metadata and provenance database** — a
content-addressed object store, an mmCIF-derived schema, and a provenance DAG,
usable embedded (SQLite/PostgreSQL) or as a standalone HTTP/JSON-RPC service. It
is host-neutral: nothing under `src/mmfdb` imports a consumer application.

## Install

```bash
pip install mmfdb                      # from PyPI, once released
pip install -e ".[postgres,s3,ldap]"   # from a source checkout, with backends
```

Documentation (concepts, usage, RPC clients, deployment) is in [`docs/`](docs/);
build it with `pip install -e ".[docs]" && make -C docs html`.

## At a glance

- `import mmfdb` exposes a deliberately small API (`MFDatabase` and runtime
  configuration); domain, admin, and integration APIs live in explicit submodules.
- Runtime path/default-user configuration is `mmfdb.config`, with `MMFDB_*`
  environment variables for embedding and standalone tests.
- The package is **host-neutral**: nothing under `src/mmfdb` imports any consumer
  application (enforced by `tests/test_architecture.py`).
- Versioned API dispatch creates one request-scoped database/auth context rather
  than reopening and reauthenticating inside each function.

## Administration UI

<p align="center">
  <img src="assets/mmfdb-admin.png" width="840"
       alt="MMFDB optical-components admin: the Alexa Fluor dye ladder with overlaid excitation and emission spectra from 350 to 800 nm.">
</p>

The optical-components surface curates fluorophores, filters, dichroics, detectors,
and light sources, overlaying their excitation/emission spectra (here the Alexa
Fluor ladder) — with approval, duplicate-finding, and reference-set import. The
same handlers back the standalone web administrator (see
[`docs/webadmin.md`](docs/webadmin.md)).

## Standalone service (Docker Compose)

From the repository root, start a persistent local MMFDB instance with:

```bash
docker compose up --build -d
curl http://127.0.0.1:8080/healthz
```

The local profile is deliberately bound to loopback and bootstraps the default
login `admin` / `admin`. Change it without editing YAML:

```bash
MMFDB_ADMIN_PASSWORD='Replace-this1!' docker compose up --build -d
```

Bootstrap is one-shot: after an active administrator exists, restarts preserve
its current password and ignore the bootstrap secret. To apply a changed
bootstrap password, start with a genuinely new data volume. Do not expose the
local `admin` / `admin` profile on a public interface.

The service provides:

- browser administration at `http://127.0.0.1:8080/login`;
- JSON-RPC at `http://127.0.0.1:8080/rpc`;
- health/readiness at `http://127.0.0.1:8080/healthz`;
- persistent SQLite and object data in the `mmfdb-data` volume.

The image runs as a non-root user with a read-only root filesystem in Compose.
The base image digest and production runtime dependencies are pinned in
`Dockerfile` and `docker-constraints.txt`; update them together and verify a
clean multi-architecture build.

## YAML configuration

[`mmfdb.example.yaml`](mmfdb.example.yaml) is the generic standalone template;
[`mmfdb.compose.yaml`](mmfdb.compose.yaml) is the explicitly local-only profile.
Validate or initialize a configuration without Docker:

```bash
export MMFDB_ADMIN_PASSWORD='Replace-this1!'
mmfdb check-config --config mmfdb.example.yaml
mmfdb init --config mmfdb.example.yaml
mmfdb serve --config mmfdb.example.yaml
```

Configuration version 1 has these top-level sections:

- `mode`: `embedded` or `standalone`; standalone is additive and does not remove
  MMFDB's in-process (embedded) use;
- `server`: HTTP bind host and port;
- `database`: exactly one of a SQLite `path` or server `url`;
- `object_store`: local root or S3-compatible target;
- `auth`: local or LDAP provider settings;
- `admin`: first-admin bootstrap user/password and the explicit weak-password
  development gate;
- `client`: `embedded`/`remote` mode, base URL, and username hints reusable by
  an embedding application. Client passwords are intentionally not persisted. Client processes
  use `mmfdb.load_client_config`, which does not resolve unrelated server-side
  admin, LDAP, database, or object-store secrets.

Relative database/object paths resolve relative to the YAML file. Unknown keys,
invalid ports, conflicting database targets, missing environment variables, and
weak bootstrap passwords fail closed. String values support `${NAME}` and
`${NAME:-local-default}` interpolation. Prefer `${NAME}` for secrets so generic
deployment files do not store credentials.

Remote client URLs must use HTTPS unless the host is loopback. Plain HTTP on an
isolated development network requires `client.allow_insecure_http: true`;
embedded credentials, query strings, and fragments are never accepted in the
base URL.

The YAML password is only a first-admin bootstrap input. It is excluded from
configuration object representations, never emitted by CLI JSON, and never
resets an established administrator on restart.

## Object storage

MMFDB defaults to its atomic local content-addressed store under
`$MMFDB_SETTINGS_DIR/objects`. An S3-compatible backend can be selected without
changing repository/API callers:

```text
MMFDB_OBJECT_STORE_BACKEND=s3
MMFDB_S3_BUCKET=research-data
MMFDB_S3_PREFIX=mmfdb/blobs                 # optional
MMFDB_S3_ENDPOINT_URL=https://minio.example # optional for AWS S3
MMFDB_S3_REGION=eu-central-1                # optional
```

The S3 backend uses conditional create, streams payloads, verifies every object
against both its content address and SHA-256 metadata, and materializes remote
objects through an atomic verified local cache. It imports `boto3` only when S3
is selected; deployments enabling S3 must provide `boto3` in their runtime.
Uploads currently use S3's conditional single-object operation and therefore
reject payloads larger than 5 GiB with an explicit error; multipart publishing
must be added before MMFDB is used for larger individual files.

MMFDB deliberately has no access-key or secret-key setting. Credentials are
resolved by boto3's standard AWS provider chain (environment variables, shared
AWS config, web identity, workload/container identity, or instance role), so
secrets are never serialized into MMFDB runtime configuration or database rows.

## SQL database targets

Local SQLite remains the zero-configuration backend. Set
`MMFDB_DATABASE_URL` (or `configure_runtime(database_url=...)`) to select a
server database explicitly:

```text
MMFDB_DATABASE_URL=postgresql://user:password@db.example.org/mmfdb
```

Install `mmfdb[postgres]` for SQLAlchemy and the Psycopg driver. MMFDB never
interprets an unknown or malformed URL as a local filename, and credentials are
redacted from backend diagnostics.

PostgreSQL runtime connectivity supports parameterized repository queries,
mapping rows, nested savepoint transactions, schema-version validation, and
dictionary DAO introspection. The server must currently be provisioned at the
package's exact schema version by the deployment system. Automatic fresh-schema
bootstrap, historical migrations, SQLite PRAGMAs, and SQLite online backups are
intentionally rejected with `DatabaseCapabilityError`; use native PostgreSQL
migration and backup tooling. This is an explicit prerelease boundary, not a
claim that SQLite DDL is portable.
