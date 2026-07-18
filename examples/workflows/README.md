# Example MMFDB workflows

A **workflow** is a plain-YAML file that names its raw *sources*, a sequence of
*steps* that each run one tool (a command-line program or a Python callable), and
a *publish* block. MMFDB coordinates the run: it registers the sources, executes
each step, and records the whole thing in its provenance graph
(`source → tool → result`), then exports the run for publication. `src/mmfdb`
never runs a tool of its own — it drives whatever the YAML names.

See {doc}`../../docs/usage` and the API in `mmfdb.workflow`
(`load_workflow`, `run_workflow`, `export_publication`).

## The examples

| File | Shows | Runs in tests? |
| --- | --- | --- |
| `01_quickstart.yaml` | one source, one command-line step, one report | ✅ |
| `02_chained.yaml` | a step's output feeding the next step | ✅ |
| `03_python_step.yaml` | an in-process Python step (`python: file.py:callable`) | ✅ |
| `04_mixed_cli_python.yaml` | a CLI tool **and** a Python tool in one run | ✅ |
| `05_fan_in.yaml` | a multi-input step (two replicates merged) | ✅ |
| `06_smfret_tttrlib.yaml` | a real tttrlib burst-search pipeline | validate-only¹ |
| `07_release.yaml` | package a **set** of results into one self-contained CIF | ✅ |

¹ `06` uses a real scientific tool (`tttrlib`) and a real measurement file, so it
is parsed and order-checked on every test run but only *executes* once you
install `tttrlib` and point its source `path` at a TTTR/SPC file. `01–05` are
self-contained (standard-library tools + the small CSVs in `data/`) and are
executed end to end, exported, and checked by `tests/test_workflow_examples.py`.

## Running one

```bash
# validate without running anything
mmfdb workflow validate examples/workflows/02_chained.yaml

# run it: --workdir holds step outputs, --output-dir gets the publish artifacts
mmfdb workflow run examples/workflows/02_chained.yaml \
    --database /tmp/mmfdb.sqlite --workdir /tmp/wf --output-dir /tmp/wf/publish

# re-export from a recorded run given a seed artifact id
mmfdb workflow export examples/workflows/02_chained.yaml \
    --database /tmp/mmfdb.sqlite --seed <artifact-id> --output-dir /tmp/wf/publish
```

Or from Python:

```python
from mmfdb.repository import MFDatabase
from mmfdb.workflow import load_workflow, run_workflow, export_publication

wf = load_workflow("examples/workflows/02_chained.yaml")
with MFDatabase("mmfdb.sqlite") as db:
    run = run_workflow(wf, db, workdir="run/")
    export_publication(db, run.seed_artifact_id, "run/publish", wf.publish.formats, name=wf.name)
```

## Anatomy of a step

```yaml
steps:
  - id: select                      # unique; referenced downstream as "select.<output>"
    operation_type: burst_selection # recorded on the operation (mmCIF vocabulary)
    software: {package: my_tool, version: "1.0"}   # recorded for citation
    inputs: {table: bursts}         # local name -> ref: a source or "step.output"
    params: {min: 0.5}              # recorded in the operation settings
    cmd: >                          # a subprocess step ...
      python {workflow_dir}/tools/select_bursts.py {inputs.table}
      --min {params.min} --output {outputs.selected}
    # python: tools/select_bursts.py:run   # ... or an in-process callable instead
    outputs:
      selected: {kind: burst_table, path: selected.csv}
```

Placeholders filled at run time: `{inputs.X}`, `{outputs.Y}`, `{params.Z}`,
`{workdir}` (where outputs are written), and `{workflow_dir}` (this file's
directory — use it for tool paths so they resolve wherever `--workdir` points).

A `python:` target is `module:callable` (imported from `sys.path`) or
`path/to/file.py:callable` (loaded relative to this YAML). The callable receives a
`StepContext` with `.inputs` / `.outputs` (`dict[str, pathlib.Path]`), `.params`
(`dict`), `.workdir`, and `.workflow_dir`; it writes its declared outputs and may
return a JSON-serializable summary dict, which is recorded in the operation
settings and surfaced in the methods report.

## Publishing

The `publish` block seeds MMFDB's provenance graph at one node and emits any of:

- `cif` — **one** self-contained, reproducible mmCIF file (`<name>.deposit.cif`):
  FLR metadata + the provenance graph + the workflow definition + every data file
  embedded (base64), all described by the shipped `mmfdb_workflow_ext.dic`
  (referenced via `_audit_conform`). A single standards text file; unpack it with
  `mmfdb workflow extract`.
- `report` — a Markdown methods section (every tool, version, command line, and
  parameter set, in execution order),
- `mmcif` — a standards mmCIF/FLR metadata file (no embedded data),
- `bundle` — a **readable** deposition ZIP that needs no MMFDB or database to
  open: mmCIF metadata + a JSON provenance graph + the methods report + a
  checksummed manifest + every data file kept in its original format. Uploads
  as-is to Zenodo/OSF, and
- `archive` — the same bundle *plus* an embedded SQLite `database_snapshot.db`,
  for exact re-import into another MMFDB instance.

Seed at a **source** for a linear pipeline (the report then walks everything
downstream), or at a **`step.output`** when several branches converge (as in
`05_fan_in`, so the report captures both replicates upstream).

### Releasing a set as one CIF file

`07_release.yaml` is the publication recipe: list the members of the set as
sources, run one multi-input step (`make_release`) that writes a manifest over
them, and `publish` a `cif` seeded at that manifest. Because the manifest
operation links every member as an input, the single CIF captures the **whole
set** — the FLR metadata, the full provenance graph, the workflow definition, and
every member's data file embedded base64 — in one standards text file that needs
no MMFDB or database to read.

```bash
mmfdb workflow run examples/workflows/07_release.yaml \
    --database mmfdb.sqlite --workdir release/ --output-dir release/publish/
# -> release/publish/release.deposit.cif   (upload as-is to Zenodo/OSF)

# unpack it anywhere — recovers the inputs, the workflow YAML, and the dictionary
mmfdb workflow extract release/publish/release.deposit.cif --output-dir unpacked/
```

The CIF is self-describing: its categories are defined in the shipped
`mmfdb_workflow_ext.dic` (also embedded in the file), and declared through an
`_audit_conform` loop, so any mmCIF reader (e.g. python-ihm, gemmi) parses it. It
is **reproducible**: `mmfdb workflow extract` recovers every original data file
byte-for-byte plus the `workflow.yaml`, which you can re-run with
`mmfdb workflow run`.

```text
release.deposit.cif
  data_… (FLR metadata)                     standard flrCIF block
  data_mmfdb_deposit
    _audit_conform                          declares mmfdb_workflow_ext.dic
    _mmfdb_workflow / _mmfdb_workflow_step   the workflow and its steps
    _mmfdb_provenance_operation/artifact/edge  the source → tool → result graph
    _mmfdb_bundle_file                       every data file + workflow.yaml + the .dic (base64)
    _mmfdb_bundle_document                   methods.md + manifest.json
```

Raw measurements (TTTR/SPC/HDF5, etc.) are embedded verbatim (base64 of the exact
bytes), so the deposit keeps the community-standard file the instrument produced.
Prefer a ZIP or split files instead? Set `formats: [bundle]` (readable ZIP),
`[archive]` (ZIP + SQLite snapshot for MMFDB re-import), or combine them.

## The tools (`tools/`)

Small, dependency-free (standard-library) command-line programs that stand in for
real analysis software, so the examples run anywhere. Each reads a CSV, does one
thing, writes a CSV, and prints a JSON summary to stdout.

| Tool | Does |
| --- | --- |
| `select_bursts.py` | keep rows whose column is within a range |
| `histogram.py` | bin a column into a histogram |
| `summary_stats.py` | count / mean / std / min / max of a column |
| `population_split.py` | split a distribution at a threshold (also exposes `run(ctx)` for a Python step) |
| `merge_tables.py` | concatenate two tables (used by the multi-input example) |
| `make_release.py` | write a manifest over a set of members (used by the release example) |

`data/` holds the small burst tables the runnable examples consume
(`bursts.csv`, `bursts_rep1.csv`, `bursts_rep2.csv` — two FRET populations around
0.28 and 0.72).

## Sample methods report

Exporting `04_mixed_cli_python.yaml` produces, for example:

```markdown
### Step 2: burst_selection

Performed with **select_bursts 1.0**.

    python .../tools/select_bursts.py .../bursts.csv --column size --min 0 --output .../selected.csv

Parameters: `min_size` = 0.
Reported: column = size, n_in = 50, n_out = 50.

### Step 3: analysis

Performed with **population_split 1.0**.

Parameters: `column` = proximity_ratio, `threshold` = 0.5.
Reported: high_fraction = 0.52, high_mean = 0.719438, low_fraction = 0.48, low_mean = 0.285092.
```
