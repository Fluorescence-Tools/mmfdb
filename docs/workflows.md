# Workflows

Define an analysis pipeline in a plain-YAML file — its raw *sources*, a sequence
of *steps* that each run one tool (a command-line program or a Python callable),
and a *publish* block — and MMFDB coordinates the run: it registers the sources,
executes each step, and records the whole thing in its provenance graph
(`source → tool → result`), then exports the run for publication. Nothing under
`src/mmfdb` runs a tool of its own; the runner drives whatever the YAML names, so
MMFDB stays host-neutral.

The Python API is `mmfdb.workflow`; the CLI is `mmfdb workflow`. A worked set of
examples lives in [`examples/workflows/`](../examples/workflows) with its own
`README.md`.

## A workflow file

```yaml
version: 1
name: smfret-burst-analysis
description: Stitch a tttrlib CLI and a Python step on one measurement.

sources:
  photons:                          # a raw input, registered before any step
    path: data/measure.spc
    kind: raw_measurement
    metadata: {file_type: SPC-130}

steps:
  - id: bursts
    operation_type: burst_selection # recorded on the operation (mmCIF vocabulary)
    software: {package: fret_burst_tool, version: tttrlib-0.27}
    inputs: {photons: photons}      # local name -> ref: a source or "step.output"
    params: {min_photons: 20}       # recorded in the operation settings
    cmd: >                          # a subprocess tool ...
      python {workflow_dir}/tools/fret_burst_tool.py {inputs.photons}
      --min-photons {params.min_photons} --output {outputs.table}
    # python: tools/adapter.py:run  # ... or an in-process callable instead
    outputs:
      table: {kind: burst_table, path: bursts.csv}

publish:
  seed: photons                     # seed the provenance graph here
  formats: [cif]                    # see "Publishing" below
```

**Placeholders** filled at run time in a `cmd`: `{inputs.X}`, `{outputs.Y}`,
`{params.Z}`, `{workdir}` (where outputs are written), and `{workflow_dir}` (the
YAML's directory — use it for tool paths so they resolve wherever `--workdir`
points).

**Python steps** target `module:callable` (imported from `sys.path`) or
`path/to/file.py:callable` (loaded relative to the YAML). The callable receives a
`StepContext` (`.inputs` / `.outputs` as `dict[str, pathlib.Path]`, `.params`,
`.workdir`, `.workflow_dir`), writes its declared outputs, and may return a
JSON-serializable summary dict that is recorded in the operation settings.

**References and ordering**: a step input is a source name or a `step.output`.
The runner orders steps by these data dependencies and rejects cycles, duplicate
ids, unknown references, and unknown keys at load time — before touching the
database.

## CLI

```bash
mmfdb workflow validate WORKFLOW.yaml            # parse + order-check only
mmfdb workflow run      WORKFLOW.yaml \           # execute, record provenance, publish
    --database mmfdb.sqlite --workdir run/ --output-dir run/publish/
mmfdb workflow export   WORKFLOW.yaml \           # re-export from a recorded run
    --database mmfdb.sqlite --seed <artifact-id> --output-dir run/publish/
mmfdb workflow extract  DEPOSIT.deposit.cif \     # unpack a single-CIF deposit
    --output-dir unpacked/
```

From Python:

```python
from mmfdb.repository import MFDatabase
from mmfdb.workflow import load_workflow, run_workflow, export_publication

wf = load_workflow("workflow.yaml")
with MFDatabase("mmfdb.sqlite") as db:
    run = run_workflow(wf, db, workdir="run/")
    export_publication(db, run.seed_artifact_id, "run/publish", wf.publish.formats,
                       name=wf.name, workflow=wf, workflow_yaml=open("workflow.yaml").read())
```

## How a run maps to provenance

Each step becomes one `mmfdb_operation` node linking its input artifacts to the
outputs it produced, stamped with the tool, version, command line, parameters,
and timing. Registration of the raw sources is attributed to MMFDB itself. The
result is that the workflow's DAG *is* the provenance graph — queryable with the
same `export_provenance_graph` everything else uses, and exportable for a paper.

The rendered invocation and the process exit status are **columns** —
`mmfdb_operation.command_line` and `mmfdb_operation.exit_code` — not entries in
the free-form `settings_json`, so they can be queried directly:

```sql
SELECT operation_id, command_line FROM mmfdb_operation WHERE exit_code != 0;
```

`exit_code` is `NULL` for an in-process `python:` step, which has no process to
exit. A tool run outside a workflow — a bare CLI call recorded by hand — should
use the `external_tool` operation type with the same two columns.

## Publishing

The `publish` block seeds the provenance graph at one node — a **source** for a
linear pipeline (the export then walks everything downstream), or a
**`step.output`** when branches converge — and emits any of:

| Format | Output | What it is |
| --- | --- | --- |
| `cif` | `<name>.deposit.cif` | **One** self-contained, reproducible mmCIF: FLR metadata + provenance graph + the workflow definition + every data file embedded (base64), described by the shipped `mmfdb_workflow_ext.dic` and declared via `_audit_conform`. |
| `report` | `<name>_methods.md` | A Markdown methods section: every tool, version, command line, and parameter set, in order. |
| `mmcif` | `<name>.cif` | A standards mmCIF/FLR metadata file (no embedded data). |
| `bundle` | `<name>.zip` | A readable ZIP that needs no database: mmCIF + JSON provenance + methods + a checksummed manifest + originals in their native format. |
| `archive` | `<name>_archive.zip` | The `bundle` plus a SQLite `database_snapshot.db` for exact re-import into another MMFDB instance. |

### The single-CIF deposit

`cif` is the recommended deposition: one standards text file, readable without
MMFDB or any database. Its categories are defined in the shipped
`mmfdb_workflow_ext.dic` dictionary (`mmfdb_workflow`, `mmfdb_workflow_step`,
`mmfdb_provenance_operation` / `_artifact` / `_edge`, `mmfdb_bundle_file`,
`mmfdb_bundle_document`), which is referenced via `_audit_conform` and embedded in
the file itself. Any mmCIF reader (python-ihm, gemmi) parses it.

It is **reproducible**: `mmfdb workflow extract` recovers every original data file
byte-for-byte plus the `workflow.yaml`, which re-runs with `mmfdb workflow run`.
Raw measurements (TTTR/SPC/HDF5, …) are embedded verbatim (base64 of the exact
bytes), so the deposit keeps the community-standard file the instrument produced.
For very large raw files, the `bundle`/`archive` ZIP avoids base64's ~33% size
overhead — the choice is one line in `publish.formats`.
