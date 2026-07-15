# Concepts — the MMFDB data model

MMFDB stores three kinds of thing and the links between them. Everything else
(the RPC methods, deposition, mmCIF export) is built on this small model.

## Object store

Raw measurement bytes live in a **content-addressed object store**: each blob is
keyed by its `md5`/`sha256`, so identical content is stored once (deduplicated).
`put_object` returns an **object UUID**; `get_object` returns the bytes. The
backend is pluggable — a local directory by default, or S3 (`object_store_backend:
s3`). Native instrument files are kept as-is; MMFDB never rewrites them.

## Artifacts

An **artifact** is a registered *data node*: a raw measurement, a burst table, a
fit result, a spectrum. It records an `artifact_id`, an `artifact_kind`, a
checksum, a size, and either a `file_path` or an object reference. Artifacts are
the nodes the provenance graph connects.

## Operations

An **operation** is a *run of a tool*. It records the software
(`software_package`, `software_version`, `software_module`), typed
**parameters**, a lifecycle `status` (`pending` → `running` → `succeeded` /
`failed`), timestamps, and an optional operator. Any tool qualifies — a Python
library, a shell script, your own code — which is why external tools integrate
without lock-in.

## The provenance graph

An operation **consumes input artifacts** and **produces output artifacts**.
Each link is a directed edge (`input_to`, `produced`), so the whole chain from
raw photons to a published figure is a queryable DAG:

```text
raw measurement --input_to--> operation (tool) --produced--> result
```

Query it with `provenance.dependencies.upstream` / `.downstream` (walk one step),
`provenance.edges.list`, or `provenance.graph.export` (the whole subgraph seeded
at a node). Two tools run on the same raw measurement simply attach two
operations to the same input node.

## Metadata and mmCIF

The scientific-metadata tables (samples, experiments, probes, FRET restraints, …)
are **generated from mmCIF dictionaries** (`data/*.dic`, the wwPDB/PDB-IHM family
plus a local FLR extension), so the schema is a standard. Fluorescence records
export back out as an mmCIF file (`export_flr_cif`), which is what makes a
deposition readable without MMFDB.

## Two ways in

- **Embedded** — `mmfdb.repository.MFDatabase` operates directly on a local
  SQLite file, no server. Best for scripts and single-machine analysis (see
  {doc}`mmfdb_08_standalone_no_lockin.ipynb <examples/mmfdb_08_standalone_no_lockin>`).
- **Server** — the standalone HTTP service exposes the same operations as
  JSON-RPC for shared/remote use (see {doc}`rpc-clients` and {doc}`webadmin`).

## Deposition

A dataset exports to a self-contained ZIP — object-store natives + a database
snapshot + the provenance graph + an mmCIF metadata file — that another MMFDB
instance re-imports or that you upload to Zenodo/OSF. See
{doc}`mmfdb_07_deposition.ipynb <examples/mmfdb_07_deposition>`.
