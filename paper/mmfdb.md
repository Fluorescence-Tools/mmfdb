---
title: "MMFDB: provenance-complete storage and standards-based deposition for multimodal fluorescence spectroscopy"
author:
  - name: Thomas-Otavio Peulen
    affiliation: "TU Dortmund University, Dortmund, Germany [TODO: confirm full affiliation and postal address]"
    email: thomas.peulen@tu-dortmund.de
bibliography: mmfdb.bib
subject: "Bioinformatics — Application Note (Data and text mining / Structural bioinformatics)"
---

<!--
Target: Bioinformatics Application Note. Hard limits: two published pages,
~1300-1500 words of body text, one figure, and a short reference list.

Conventions used in this draft:
  * Every quantitative claim is measured from the repository at commit ce2560f.
    Commands that reproduce them are given in "Reproducing the numbers" at the end,
    which is scaffolding for the authors and is NOT part of the submission.
  * [TODO: ...] marks something that must be supplied or measured before
    submission. Nothing has been invented to fill a gap.
-->

## Abstract

**Motivation:** Multi-laboratory benchmarks have shown that single-molecule FRET
yields reproducible distances and kinetics when the analysis is specified
precisely, and the FLR extension of PDBx/mmCIF now standardises how fluorescence
metadata are deposited. What is routinely lost is the specification itself:
which tool, at which version, with which parameters, was applied to which
photons.

**Results:** MMFDB is a multimodal fluorescence metadata and provenance
database combining a content-addressed object store that keeps native instrument
files byte-for-byte, a relational schema generated from the community mmCIF
dictionaries rather than hand-written, and a provenance graph linking every
derived result to the tool run that produced it. MMFDB records what a tool did
rather than prescribing which tool to use — a command-line program, a
third-party package or a local script is registered identically, and no module
in the package imports any analysis application. A declarative YAML workflow
becomes that same graph and exports as one self-describing mmCIF file embedding
the data, the provenance and the workflow, readable without MMFDB.

**Availability and implementation:** MMFDB is implemented in Python (≥3.10),
released under the MIT licence, and available at
<https://github.com/Fluorescence-Tools/mmfdb> with documentation, nine worked
example notebooks, and seven example workflows.
[TODO: add PyPI release and archived Zenodo DOI before submission — the package
is not yet on PyPI.]

**Contact:** thomas.peulen@tu-dortmund.de

**Supplementary information:** Supplementary data are available at
*Bioinformatics* online.

## 1 Introduction

Single-molecule FRET has become a quantitative structural technique. Two
multi-laboratory benchmark studies showed that independent groups recover
consistent inter-dye distances and dynamic parameters, provided the correction
factors and the analysis procedure are stated exactly [@hellenkamp2018;
@agam2023], and the community has argued for open-science practices that make
fluorescence analyses inspectable [@lerner2021]. The deposition side is solved in
principle: the FLR extension of PDBx/mmCIF, developed within IHMCIF, defines how
fluorescence-based structures and their kinetic information are archived
[@hanke2024; @vallat2024; @westbrook2022].

The gap is what happens between the instrument and the deposition. Photon data
are analysed with a heterogeneous set of tools — FRETBursts, PAM, ChiSurf and
libraries such as tttrlib [@ingargiola2016fretbursts; @schrimpf2018;
@peulen2025chisurf; @peulen2025tttrlib] — exchanged through formats such as
Photon-HDF5 [@ingargiola2016photonhdf5], and the link between a raw photon record
and a published number survives only as prose. Existing infrastructure solves
adjacent problems: workflow systems record execution but carry no fluorescence
semantics [@crusoe2022; @molder2021; @ditommaso2017], electronic lab notebooks
record experimental context but not the data dependency graph [@carpi2017], and
packaging conventions bundle artefacts without modelling the measurement
[@soilandreyes2022]. None answers what a reviewer actually asks: which photons,
which tool, which version, which parameters.

MMFDB addresses that gap as a storage and provenance layer — deliberately not an
analysis framework — that makes the tool run a first-class queryable record and
emits the community standard on the way out.

## 2 Implementation

### 2.1 Data model and provenance

MMFDB stores two kinds of node and the edges between them (Fig. 1b). An
**artifact** is a data node — a raw measurement, a burst table, a fit result —
carrying a kind, a checksum, a size and a file or object-store reference. An
**operation** is one run of one tool, carrying software package, version and
module, typed parameters, a lifecycle status, timestamps and an operator. An
operation consumes inputs (`input_to`) and produces outputs (`produced`), making
the chain from photons to result a queryable directed acyclic graph; a separate
table carries twelve non-execution relationships such as `derived_from`,
`supersedes` and `calibrated_by`.

Three design choices matter for reproducibility. The rendered invocation and the
process exit status are columns of the operation table rather than entries in a
free-form settings blob, so `SELECT operation_id, command_line FROM
mmfdb_operation WHERE exit_code != 0` is a direct query. Registration is atomic:
object write, artifact, operation, ports, edges and parameters commit in one
transaction, with a rollback hook that removes a newly published blob if that
transaction fails. And because SQLite silently coerces `NaN` to `NULL` in a real
column, MMFDB records which numeric parameter fields held `NaN` and restores them
on read — a diverged fit must not become indistinguishable from an uncomputed
one.

Raw bytes live in a content-addressed object store keyed by MD5 for routing and
verified by SHA-256, so identical content is stored once and every read is
re-verified against both digests. Native instrument files are never rewritten,
and the backend is pluggable: an atomic local directory by default, or an
S3-compatible service.

Calibration handling makes the graph scientifically load-bearing. Correction
factors that every downstream FRET efficiency inherits — γ, crosstalk, direct
excitation, the *G*-factor, the donor lifetime and *R*₀ — are registered as
calibration artifacts referenced by a `calibrated_by` edge, so a re-measured
calibration resolves to exactly which recorded results used the superseded value:
the machine-checkable form of a correction notice.

### 2.2 A schema generated from the community dictionaries

The scientific-metadata schema is not hand-authored. MMFDB ships nine mmCIF
dictionaries (21.3 MB of DDL2 text: the wwPDB DDL, core and PDBx v5
dictionaries, ModelCIF, the PDB-IHM integrative-modelling and FLR extensions, and
two locally authored extensions). Eight are merged into the runtime dictionary,
together defining 975 categories and 9,343 items, of which 497 carry a schema
binding mapping a dictionary item onto a table and column. Applying them produces
75 tables and 817 columns at schema version 47; 20 of those tables exist *only*
because the dictionary declares them, and migration is implemented by replaying
the dictionary against the live database. Enumerations are seeded into a
vocabulary table as 190 controlled terms across 21 fields, including 54 artifact
kinds, 35 operation types and 12 relationship types. Closed vocabularies reject
unknown values; open-ended ones such as `data_format` record new values as
non-builtin terms rather than failing.

Two tiers must be distinguished. Scientific metadata bind to categories of the
PDB-IHM FLR dictionary — sample, probe list, probe positions, Förster radii and
sample conditions — extended with defaults of scientific consequence such as
κ² = 2/3. Provenance, by contrast, is typed by a **prototype** extension authored
in the same DDL2 form and declared through `_audit_conform`: the operation,
artifact and edge categories and their vocabularies are ours, not the community's.
Its versatility is what has been demonstrated rather than its adoption — the
parameter registry already spans fluorescence correlation spectroscopy (85
items), time-correlated single-photon counting (27), raster image correlation
spectroscopy (11) [@digman2005] and image correlation (1) alongside FRET,
anisotropy and photon-budget quantities, so it absorbs the parameter sets of
tools built independently of it. Proposing it to the FLR working group is future
work.

### 2.3 Workflows and deposition

A pipeline is declared in one YAML file: raw *sources*, *steps* that each run one
tool as a subprocess (`cmd:`) or an in-process callable (`python:`), and a
*publish* block (Fig. 1c). The file is fully validated — unknown keys, duplicate
identifiers, unresolved references and dependency cycles are rejected — before
the database is touched. Each step becomes exactly one operation node, so the
workflow's dependency graph *is* the provenance graph. Nothing in MMFDB runs an
analysis of its own; the runner executes whatever the YAML names.

Publication emits any of five formats. The recommended one is a single
self-describing mmCIF file — a standards-conformant container carrying the
prototype provenance extension — that holds the provenance graph, the workflow
definition, and every data file base64-embedded, organised into seven categories
defined by a shipped dictionary that is itself declared through `_audit_conform`
and embedded in the file. The deposit therefore carries the means of its own
interpretation: any mmCIF reader parses it [@pythonihm; @wojdyr2022] with no
database and no MMFDB installed, and `mmfdb workflow extract` recovers every
original file byte-for-byte together with the YAML, which re-runs. A ZIP bundle
and a Markdown methods section listing every tool, version, command line and
parameter set in order are the alternatives.

### 2.4 Deployment

MMFDB runs embedded against a local SQLite file with no server, or standalone as
a dependency-free WSGI service exposing JSON-RPC 2.0 (222 registered methods, 40
of them in a versioned family sharing one request-scoped database and
authentication context), a streaming object endpoint and a browser administration
interface. Direct Python calls and remote calls pass the same authenticated
boundary; there is no trusted unauthenticated in-process mode. PostgreSQL,
S3-compatible storage and LDAP are optional backends whose capabilities are
declared explicitly, so an unsupported operation fails before any destructive
statement rather than assuming SQL portability. The core package depends on four
runtime libraries.

## 3 Results

<!-- [TODO: numbers in this paragraph are from the current notebook 06, which is
driven by a host application. Per the agreed plan the notebook is being rewritten
to run standalone (tttrlib.SimEngine for simulation, embedded MFDatabase instead
of the host's RPC client) and MUST be re-executed. Burst counts and mean
efficiencies will shift; update this paragraph and fig2_provenance_dag.svg from
the re-run, then rebuild figure1.svg. Do not hand-edit the numbers. -->

MMFDB's central claim is that the same measurement analysed by two independent
tools yields two independently attributable records. In the shipped example
(Fig. 1b), a simulated two-state smFRET measurement of 9 × 10⁵ photons was
registered once as a checksummed artifact and analysed twice: by a command-line
burst-search tool built on tttrlib, run as a subprocess, and by FRETBursts 0.8.3
called in process. Both runs were recorded as `external_tool` operations carrying
software version, command line, exit status, timestamps and typed parameters,
with both burst tables linked to the same raw node. The tools recovered mean FRET
efficiencies of 0.502 and 0.504 against a simulated ground truth of 0.500, while
returning 3,579 and 2,647 bursts. That discrepancy is the point: two defensible
burst-search procedures disagree by 35 % in the number of single molecules they
find while agreeing on the efficiency, and the graph records which parameters
produced which count. A second example repeats this with MMFDB used entirely on
its own, asserting that the analysis application was never imported.

Worked examples cover burst selection, burst variance analysis [@torella2011],
photon-by-photon hidden Markov modelling [@pirchi2016], fluorescence correlation
spectroscopy and confocal image reconstruction, all reading from the same object
store. Portability is shown by exporting a dataset as an 831 kB archive and
reconstituting a second, independent instance from it with artifacts and edges
intact.

Correctness is enforced rather than asserted: 758 tests pass on Python 3.10–3.12
in continuous integration. Several are executable specifications — an
abstract-syntax-tree walk over every source file forbids importing the host
analysis application, making host-neutrality a checked property, and a failed
migration must restore a pre-migration snapshot and pass an integrity check.

**Limitations.** The mmCIF round trip is not symmetric: the export writes sixteen
loops and the importer consumes nine, so Förster radii, distance restraints and
photon-stream references are export-only, with full-fidelity portability provided
instead by the deposit bundle. Re-importing a deposition requires the archive
format's database snapshot; the readable bundle and single-file deposit are
machine-readable but not yet parsed back into rows. An operation records software
package and version but not the container image or environment digest, so
bit-level re-execution is not yet guaranteed.
[TODO: performance and scalability are unmeasured. If a claim about dataset size
or query latency is wanted, benchmark ingest, provenance traversal and deposit
export on a realistic TTTR dataset and add a table.]

## 4 Conclusion

MMFDB makes the provenance of a fluorescence analysis a queryable object rather
than a paragraph, typed in a controlled vocabulary rather than in shell strings,
and publishes it in a standards-conformant container. Because it records what a
tool did instead of dictating which tool to use, it can sit under existing
analysis software without displacing it, which is the practical precondition for
the field-wide reproducibility that benchmark studies have shown to be
achievable. Work in progress includes container-based execution endpoints that
record an image digest, so a deposited workflow re-runs in its original
environment — the case for which is already concrete, since tools in routine use
require mutually incompatible numerical stacks — together with a symmetric
importer for the single-file deposit and submission of the provenance extension
to the FLR working group.

## Figure

**Fig. 1. MMFDB architecture, provenance model, and deposition.**
(**a**) MMFDB is reached embedded through a local database file, over JSON-RPC,
from the command line, or through a browser administration interface. The core
comprises a content-addressed object store, a schema generated from the
PDBx/mmCIF, IHM and FLR dictionaries, and a provenance graph; SQLite or
PostgreSQL, local or S3 storage, and local or LDAP authentication are selectable
backends. No module in the package imports a consumer application, which is
enforced by a test. (**b**) One simulated smFRET measurement, registered once as
a checksummed artifact, analysed by a tttrlib-based command-line tool run as a
subprocess and by FRETBursts 0.8.3 run in process. Each tool run is an operation
node carrying its version, command line, exit status and typed parameters; both
burst tables link back to the same raw node. The two tools agree on the mean FRET
efficiency to within 0.5 % of the simulated ground truth while differing in burst
count, and the graph records which parameters produced each. (**c**) A YAML
workflow declares sources, steps and a publish block; the runner records each
step as one operation and emits any of five formats. The recommended single-file
mmCIF deposit embeds the data, the provenance and the workflow, and is parsed by
any mmCIF reader without MMFDB.

*Figure files:* `figures/figure1.svg` is the composite; `figures/fig1_architecture.svg`,
`figures/fig2_provenance_dag.svg` and `figures/fig3_deposit_flow.svg` are the
individual panels; `figures/build_figure1.py` regenerates the composite.

## Acknowledgements

[TODO: add acknowledgements — in particular, colleagues who contributed the
curated fluorophore, spectra and reference-diffusion datasets that ship with the
package, and anyone who tested the deposition path.]

## Funding

[TODO: add funding statement and grant numbers.]

## Conflict of Interest

None declared.

## References

<!-- Rendered from mmfdb.bib. Cited above: hellenkamp2018, agam2023, lerner2021,
hanke2024, vallat2024, westbrook2022, ingargiola2016fretbursts, schrimpf2018,
peulen2025chisurf, peulen2025tttrlib, ingargiola2016photonhdf5, crusoe2022,
molder2021, ditommaso2017, carpi2017, soilandreyes2022, digman2005, pythonihm,
wojdyr2022, torella2011, pirchi2016 — 21 references. mmfdb.bib additionally
contains vallat2018, harris2022, wilkinson2016 and provdm2013, which are
available if a reviewer asks for FAIR or PROV framing but are not cited here to
stay within the Application Note length. -->

---

## Reproducing the numbers (author scaffolding — remove before submission)

Every quantitative statement above was measured against commit `ce2560f`. The
commands below regenerate them.

| Claim in the text | Command |
| --- | --- |
| 75 tables, 817 columns, schema version 47 | `python -c "import sqlite3,sys; sys.path.insert(0,'src'); from mmfdb.schema.schema import migrate_schema,SCHEMA_VERSION; c=sqlite3.connect(':memory:'); migrate_schema(c); t=[r[0] for r in c.execute(\"select name from sqlite_master where type='table' and name not like 'sqlite_%'\")]; print(SCHEMA_VERSION, len(t), sum(len(list(c.execute(f'PRAGMA table_info(\\\"{n}\\\")'))) for n in t))"` |
| 975 categories, 9,343 items, 497 schema-bound | `python -c "import sys; sys.path.insert(0,'src'); from mmfdb.schema.pdbx_metadata import MmcifDictionary as M; d=M.load_bundled(); print(len(d._categories), len(d._items), sum(1 for i in d._items.values() if i.schema_table or i.schema_column))"` |
| 190 vocabulary terms across 21 fields | `select count(*), count(distinct field_name) from mmfdb_vocabulary` on a freshly migrated database |
| 54 artifact kinds, 35 operation types, 12 relationship types | `python -c "import sys; sys.path.insert(0,'src'); from mmfdb import models; print(len(models.ARTIFACT_KINDS), len(models.OPERATION_TYPES), len(models.RELATIONSHIP_TYPES))"` |
| 222 JSON-RPC methods, 40 versioned | `registered_service_names()` and `VERSIONED_MMFDB_METHODS` in `src/mmfdb/admin/backend/services.py` |
| 758 tests passing | `PYTHONPATH=src PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -q` (one skip when the optional `ldap3` extra is absent) |
| 3,579 / 2,647 bursts, mean *E* 0.502 / 0.504 | stored outputs of `examples/mmfdb_06_external_tool_provenance.ipynb` |
| 831 kB deposition archive | stored outputs of `examples/mmfdb_07_deposition.ipynb` |
| 9 dictionaries, 21.3 MB | `wc -c src/mmfdb/data/*.dic` |

### Discrepancies found while drafting — resolve before submission

These are documentation or repository issues, not manuscript errors. They were
found while verifying claims and are recorded here so they are not lost.

1. `docs/index.md` and `docs/usage.md` state that the example notebooks are
   "executed on every test run, so its rendered outputs are real". No test in
   this repository executes a notebook; that test lives in the downstream
   application's repository. The stored notebook outputs used above are real but
   are not regenerated by this package's CI. Either add a notebook-execution test
   or soften the claim.
2. Notebooks 00–05 create object-store blobs only — no artifacts, operations or
   provenance edges. Only notebooks 06, 07 and 08 build a provenance chain. The
   manuscript above therefore draws its worked example from 06 and 08 only.
3. Notebooks 00–07 import the downstream analysis application; only notebook 08
   is genuinely standalone. This weakens the "no analysis framework required"
   framing if a reviewer runs the examples.
4. In `examples/mmfdb_04_fcs.ipynb` the reported amplitude corresponds to
   ~0.03 molecules in the focal volume, which contradicts the configured
   population of 1.5, and the amplitude is read from the first non-zero lag
   rather than an extrapolated *G*(0). This number is deliberately not cited
   above; the notebook should be corrected or annotated.
5. `examples/` contains stray SQLite files named `None`, `None-shm` and
   `None-wal` (~2.8 MB), created by a run with an unset database path, plus an
   untracked `examples/backups/` holding three further copies. `docs/examples` is
   a symlink to `examples/`, so these ship into the documentation tree. Remove
   before releasing.
6. There is no `CITATION.cff`. Add one so the software is citable alongside this
   paper.
