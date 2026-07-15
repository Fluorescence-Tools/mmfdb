# Installing MMFDB

MMFDB is a standalone Python package with no required analysis framework. Install
it with `pip`.

## Install

```bash
pip install mmfdb            # from PyPI, once released
```

or from a source checkout (in the package directory):

```bash
pip install -e .
```

Optional backends and tools are extras — install only what you need:

```bash
pip install "mmfdb[postgres]"   # PostgreSQL instead of the default SQLite
pip install "mmfdb[s3]"         # S3-compatible object store
pip install "mmfdb[ldap]"       # LDAP authentication
pip install "mmfdb[docs]"       # build this documentation
```

## Run it

**Embedded** — a local SQLite file, no server:

```python
from mmfdb.config import configure_runtime
from mmfdb.repository import MFDatabase

configure_runtime(database_path="lab.db", settings_dir=".mmfdb")
with MFDatabase("lab.db") as db:
    db.register_artifact(artifact_id="raw/1", artifact_kind="raw_measurement",
                         file_path="measurement.spc", checksum="…64 hex…")
```

**Standalone server** — HTTP + JSON-RPC for shared or remote use:

```bash
mmfdb check-config --config mmfdb.example.yaml   # validate a deployment
mmfdb init         --config mmfdb.example.yaml   # create storage + bootstrap the admin
mmfdb serve        --config mmfdb.example.yaml   # serve on :8080
```

A ready-to-run container profile ships with the package:

```bash
docker compose -f docker-compose.mmfdb.yml up -d
curl http://127.0.0.1:8080/healthz
```

## Check it works

```bash
pytest                      # run the package test suite
```

## Optional analysis libraries

MMFDB records the provenance of whatever tools you run; install those separately.
The bundled examples use `tttrlib` (photon I/O and simulation) and, optionally,
[FRETBursts](https://fretbursts.readthedocs.io):

```bash
pip install tttrlib fretbursts
jupyter lab examples/         # open the example notebooks
```

```{note}
FRETBursts 0.8.3 predates NumPy 2, so the example that uses it adds a one-line
`np.asfarray` compatibility shim before importing it, and its notebook test is
skipped if FRETBursts is absent.
```

## Consumers

Analysis applications depend on MMFDB as a library and bundle it for their users;
[ChiSurf](https://github.com/fluorescence-tools/chisurf) is one such optional
consumer. Using MMFDB requires none of them.

Next: {doc}`concepts` for the data model, {doc}`usage` for worked examples,
{doc}`rpc-clients` to build a client, {doc}`webadmin` for the web UI.
