# Using MMFDB

This page maps *what you want to do* to the **tested example notebook** that shows
how. The notebooks under `examples/` are executed on every test run, so the code
you copy has actually run — the docs point at it rather than duplicating it.

MMFDB itself is reached two ways (see {doc}`concepts`): **embedded**, through
`mmfdb.repository.MFDatabase` on a local file, or over **JSON-RPC** against a
server (see {doc}`rpc-clients`). The photon analyses below use `tttrlib` for I/O
and simulation; a few also use a single-molecule analysis toolkit's convenience
layer for the FRET-specific steps, but MMFDB stores and tracks the results the
same way regardless of which tool produced them.

## Tasks

### Start from data with a known answer

Simulate photons for FRET states you choose, store them in MMFDB, and keep the
ground truth to validate an analysis before trusting it on real data.
→ {doc}`mmfdb_00_overview.ipynb <examples/mmfdb_00_overview>` (the whole flow end to end)

### Find the single molecules (burst selection)

Run a burst search and get a table of bursts (size, duration, count rate).
→ {doc}`mmfdb_01_burst_selection.ipynb <examples/mmfdb_01_burst_selection>`

### Test for conformational dynamics (BVA)

Burst Variance Analysis compares each burst against the shot-noise limit; the
fraction sitting above it is the dynamic population.
→ {doc}`mmfdb_02_bva.ipynb <examples/mmfdb_02_bva>`

### Recover FRET states photon-by-photon (H2MM)

Fit a photon-by-photon hidden Markov model, select the number of states, and read
the per-state efficiencies, transition rates, and dwell times.
→ {doc}`mmfdb_03_h2mm.ipynb <examples/mmfdb_03_h2mm>`

### Measure diffusion / concentration (FCS)

Correlate the photon stream with `tttrlib.Correlator` to recover the diffusion
time `τ_D = w0²/4D`.
→ {doc}`mmfdb_04_fcs.ipynb <examples/mmfdb_04_fcs>`

### Reconstruct a confocal image (CLSM/FLIM)

Store a raster-scanned photon stream in MMFDB and reconstruct it with
`tttrlib.CLSMImage`.
→ {doc}`mmfdb_05_imaging.ipynb <examples/mmfdb_05_imaging>`

### Track what a tool did (provenance)

Any tool — a CLI program, a third-party package, your own script — is recorded as
a provenance operation with its name, version, command line, and checksummed
inputs/outputs, then queried as a lineage graph.
→ {doc}`mmfdb_06_external_tool_provenance.ipynb <examples/mmfdb_06_external_tool_provenance>` (drives the click CLI
{download}`external_tools/fret_burst_tool.py <../examples/external_tools/fret_burst_tool.py>` **and** FRETBursts)

### Stitch tools together with a plain-YAML workflow

Declare a pipeline in one YAML file — its raw *sources*, a sequence of *steps*
that each run a command-line tool (`cmd:`) or a Python callable
(`python: module:callable`), and a *publish* block — and let MMFDB coordinate the
run. Every step becomes one operation in the provenance graph (`source → tool →
result`), and the publish block exports the workflow for a paper: a Markdown
**methods report**, an **mmCIF/FLR** metadata file, and a self-contained
**deposition ZIP**.

```bash
mmfdb workflow validate examples/workflows/01_quickstart.yaml
mmfdb workflow run examples/workflows/04_mixed_cli_python.yaml \
    --database mmfdb.sqlite --workdir run/ --output-dir run/publish/
```

A worked set lives in {download}`examples/workflows/ <../examples/workflows>` (see
its `README.md`): `01_quickstart` (one CLI step), `02_chained` (a step feeding
the next), `03_python_step` (an in-process Python adapter), `04_mixed_cli_python`
(a CLI and a Python tool in one run), `05_fan_in` (a multi-input merge),
`06_smfret_tttrlib` (the real tttrlib pipeline, the declarative form of
{doc}`mmfdb_06_external_tool_provenance.ipynb
<examples/mmfdb_06_external_tool_provenance>`), and `07_release` (package a set of
results into **one** self-contained, reproducible mmCIF deposit for Zenodo/OSF).
Every example is validated on each test run and the self-contained ones are
executed end to end. The Python API is `mmfdb.workflow` (`load_workflow`,
`run_workflow`, `export_publication`, `write_single_cif`, `read_single_cif`).

The `cif` publish format writes a single self-describing `.deposit.cif` — FLR
metadata, the provenance graph, the workflow definition, and every data file
embedded — governed by the shipped `mmfdb_workflow_ext.dic` dictionary and
declared via `_audit_conform`. Unpack one with `mmfdb workflow extract
<file>.deposit.cif --output-dir <dir>`, which recovers the inputs and the
workflow YAML byte-for-byte so the analysis can be re-run.

### Archive or share a dataset (deposition)

Export a self-contained ZIP (native raw files + database snapshot + provenance +
mmCIF metadata), re-import it into another MMFDB instance, or upload it to
Zenodo/OSF with their own tools.
→ {doc}`mmfdb_07_deposition.ipynb <examples/mmfdb_07_deposition>`

### Use MMFDB on its own

Import `mmfdb` directly and combine any libraries (here `tttrlib` and FRETBursts)
with full provenance — no analysis framework required.
→ {doc}`mmfdb_08_standalone_no_lockin.ipynb <examples/mmfdb_08_standalone_no_lockin>`

## Keeping the docs honest

These pages hold prose and *pointers*, not copied code. When a workflow changes,
its example notebook changes and its test catches breakage; this page only needs
its one-line description updated. If you add an example, add a row to the table in
{doc}`index`, a task here, and register it in the notebook test.
