# MMFDB paper — decisions and requirements

Working document from the grilling session. Captures what was decided, what
must be built or fixed before submission, and what is still open. No code has
been written against this yet.

Status legend: **[LOCKED]** decided · **[OPEN]** still to decide · **[BLOCKED]** waiting on something

---

## 1. The claim stack **[LOCKED]**

| Layer | Decision |
| --- | --- |
| Core claim | **Tool-agnostic fluorescence provenance** — any tool's run becomes a queryable, typed node |
| Defence against "Snakemake already does provenance" | **Domain-typed provenance** — enumerated operation types and artifact kinds from an mmCIF DDL2 vocabulary, versus a workflow engine's opaque shell strings and paths |
| Dictionary maturity | **Prototype with stated intent** — a working prototype, versatile enough to absorb diverse tools' parameter vocabularies, which we intend to propose to the FLR working group. Precedent: Vallat *et al.* 2018 published a "Prototype System" in *Structure* (already in `mmfdb.bib`) |

**Why this framing and not a stronger one.** The provenance vocabulary
(`_mmfdb_artifact.artifact_kind`, `_mmfdb_operation.operation_type`,
`_mmfdb_edge.relationship_type`) is defined in the *locally authored*
`mmfdb_flr_ext.dic`, not upstream. Only `_entity.type` comes from wwPDB. Of the
44 categories in the local dictionary, 7 genuinely bind upstream PDB-IHM FLR
categories, 1 is invented (`flr_chisurf_parameter`), and 36 are `mmfdb_*`.
Claiming "typed from the wwPDB dictionary" would be false. The prototype framing
is both honest and sufficient to defeat the Snakemake comparison.

**Evidence of versatility (verified).** The 229-item fit-parameter registry
already spans method families: FCS 85, TCSPC 27, RICS 11, ICS 1, and 105 general
FRET/anisotropy/photon-budget terms.

**Known constraint to disclose.** `register_vocabulary_value` exists on
`MFDatabase` (Python) but is **not** exposed over JSON-RPC — no vocabulary
methods appear among the 222 registered. A site can extend the vocabulary
embedded; a remote client cannot.

---

## 2. Dictionary remediation **[LOCKED]** — blocks the "propose" claim

The dictionary must be in proposable shape before the paper claims intent to
propose it.

| # | Requirement | Detail |
| --- | --- | --- |
| 2.1 | Rename `flr_chisurf_parameter` | 229 item tags currently squat the community `flr_` namespace under a desktop application's brand. Proposed target: `flr_fit_parameter` (name to confirm). No live SQL table exists for this category, so **no data migration is required** — it is a naming registry only |
| 2.2 | Add a `_dictionary` header | `mmfdb_flr_ext.dic` has **zero** `_dictionary.version` tags. `mmfdb_workflow_ext.dic` has one. A proposable extension must declare title, version and datablock id |
| 2.3 | Preserve legacy spellings | Register old names in the existing `EXTENSION_CATEGORY_ALIASES` / `canonical_extension_category` mechanism in `cif_writer.py`, which already handles the earlier `_chisurf_*` → `_mmfdb_*` rename. Write canonical, read both |
| 2.4 | Update bindings | `_mmfdb_schema.table_name` / `.column_name` bindings and the vocabulary loader must follow the rename |
| 2.5 | Decide prefix policy | `mmfdb_` is a project prefix. IHMCIF uses `ihm_`, ModelCIF uses `ma_`. If proposing to FLR, consider whether provenance categories should carry a domain prefix rather than a project one. **[OPEN]** |

Scale of the branding problem: 989 lines of `mmfdb_flr_ext.dic` mention
`chisurf`, of which **922 are in identifiers**, not prose.

---

## 3. De-branding the shipped package **[LOCKED]**

Scope agreed: the provenance-facing items only, not a full de-brand. `chisurf`
appears in 19 of 95 shipped modules; the rest are docstrings and internal
references governed by the AST import test.

| # | Location | Change |
| --- | --- | --- |
| 3.1 | `queries/artifacts.py:868` | `software_package: str \| None = "chisurf"` → `None`. **This is the single line most contradictory to the paper's core claim**: the default provenance attribution in a tool-agnostic system names one application. Check whether `workflow/runner.py::_neutralize_import_software` becomes deletable afterwards |
| 3.2 | `security/credentials.py:9` | `SERVICE_NAME = "ChiSurf MMFDB"` → `"MMFDB"` |
| 3.3 | `config.py:582` | Default settings dir `~/.chisurf` → `~/.mmfdb`, falling back to `~/.chisurf` when it already exists so no user breaks. Note this touches the `conftest.py` guards that assert no test resolves state under the real `~/.chisurf` |
| 3.4 | `repository.py:821` | **Added during grilling.** `writer.start_block("chisurf_flr_export")` brands the data block of *every exported mmCIF* — the first line of every standards file MMFDB produces. Commit `ce2560f` de-branded extension categories but missed this. Rename to `mmfdb_flr_export` (or a deposit-specific block name) |

---

## 4. Evidence: the worked provenance example **[LOCKED]**

**Decision: rewrite notebook 06 to be standalone, then re-run it.**

Notebook 06 supplies the paper's headline numbers *and* imports ChiSurf, which
undercuts a tool-agnosticism claim demonstrated inside an analysis framework.
Its dependency is shallow — only `BurstWorkflow` for `simulate()`, `fetch()`,
`close()` and `_client`.

| # | Requirement |
| --- | --- |
| 4.1 | Replace `wf.simulate(...)` with `tttrlib.SimEngine.from_dict(...)` — pattern already proven in notebook 08 |
| 4.2 | Replace `wf._client` with embedded `MFDatabase` or `mmfdb.client.HttpJsonRpcClient` — also proven in 08 |
| 4.3 | Preserve what makes 06 the better evidence: `external_tool` typing, real subprocess `argv`, `command_line`, `exit_code = 0` for the subprocess versus `NULL` for the in-process run, typed parameters |
| 4.4 | **Re-run the notebook. Do not hand-edit numbers.** Current stored values (3,579 / 2,647 bursts, mean *E* 0.502 / 0.504) will shift |
| 4.5 | Update `paper/mmfdb.md` §3 and `paper/figures/fig2_provenance_dag.svg` to the re-run values, then regenerate `figure1.svg` via `build_figure1.py` |

**[BLOCKED]** Prerequisite: `fretbursts` and `phconvert` are **not installed** in
this environment (`tttrlib` 0.27.0 and NumPy 1.26.4 are present). Needs
`pip install -e ".[examples]"`. NumPy 1.26 is compatible with FRETBursts 0.8.3,
which predates NumPy 2.

---

## 5. Container execution endpoints **[DEFERRED to a follow-up]**

**Superseded.** Initially locked as "implement before submission", then deferred
when the venue decision (§7) kept the Application Note format. Recorded here in
full because the design work stands and this is the intended next step.

In the Application Note this appears only as a future-work sentence in the
Conclusion, and the existing honest limitation about environment capture stays.

The rationale, if revisited: it converts the largest declared limitation into the
strongest form of the core claim — not merely "we record what a tool did" but
"any tool, in any environment, re-executable from the recorded digest."

The motivating example is already real in this repository: FRETBursts 0.8.3
predates NumPy 2 and the notebooks carry an `np.asfarray` shim to work around
it. Two tools that cannot share a Python environment, stitched into one
provenance graph, is an honest and concrete argument.

| # | Requirement |
| --- | --- |
| 5.1 | Add a `container:` step kind beside `cmd:` and `python:` in `workflow/spec.py`. The registry is already injectable — `run_workflow(..., executors=...)`, `default_executors` is `{"cmd": ..., "python": ...}` |
| 5.2 | Record image reference **and resolved digest**. By the codebase's own design logic — `command_line` and `exit_code` are columns precisely so they are queryable — the digest should be a column, not buried in `runtime_environment_json` |
| 5.3 | Extend `Invocation` (currently `kind`, `command_line`, `returncode`, `stdout`, `summary`) with image and digest fields |
| 5.4 | Add the container reference to the deposit CIF so a deposited workflow re-runs in its original environment |
| 5.5 | Worked example: FRETBursts in a NumPy-1 container and tttrlib in another, producing one provenance graph |
| 5.6 | Decide runtime: Docker only, or also Podman/Apptainer? Apptainer matters for HPC. **[OPEN]** |
| 5.7 | Decide whether the container executor is a required dependency or an optional extra. Recommend optional — the core package currently has only four runtime dependencies, which is a stated strength **[OPEN]** |

---

## 6. The export claim **[LOCKED — revised]**

**Final decision: claim export at the deposit level.** The paper prints an
excerpt of `<name>.deposit.cif` showing real `_mmfdb_provenance_operation` rows,
the embedded dictionary and `_audit_conform`, and describes it as *a
standards-conformant mmCIF container carrying a prototype provenance extension*.
It does **not** claim flrCIF export. Zero engineering cost; this path already
works and is tested.

The near-empty FLR metadata export is simply not featured in the paper.

**Deferred to the follow-up: populating a real FLR record.**

The problem being fixed: the shipped export is ~1 kB of empty scaffolding — a
single `_flr_fret_analysis` loop with no Förster radii, no restraints, no
populated sample or probe content. **No notebook creates a FRET restraint**,
despite `flr_fret_distance_restraint` existing in the schema.

| # | Requirement |
| --- | --- |
| 6.1 | Create a genuine record: `flr_sample`, `flr_probe_list`, `flr_poly_probe_position`, `flr_fret_forster_radius`, `flr_fret_distance_restraint` |
| 6.2 | Print a real excerpt of the populated export in the paper |
| 6.3 | Full worked example plus full documentation — user's explicit requirement |

**[OPEN] — needs your scientific input.** What the record describes is not
something I can look up or invent. Awaiting the literature survey (§8) before
putting the choice back to you. Candidates raised: a dsDNA ruler matching the
Hellenkamp 2018 benchmark so the deposited restraint is checkable against a
community value; the real Becker & Hickl single-molecule DNA data already in the
repo (dye pair and construct undocumented); a simulated construct; or a record
from your own published work.

---

## 7. Venue **[LOCKED]**

***Bioinformatics* Application Note. New scope deferred.** Roughly two weeks to
submission rather than several months.

**Ships in this paper:** the provenance model, the dictionary-derived schema, the
two-tool evidence, and the deposition formats.

**Deferred to a follow-up:** container execution endpoints (§5), the populated
FLR record (§6), and formally proposing the dictionary extension.

**Consequence for the dictionary framing.** The prototype description stays —
it is honest and costs nothing — but the paper should not lean on an imminent
formal proposal it is not making. Phrase as a prototype whose versatility is
demonstrated, with community proposal named as future work.

**Consequence for length.** The draft must come down from 1,546 body words to
~1,300. Dropping the flrCIF export hook in favour of the deposit-level claim
(§6) already helps.

---

## 8. Literature survey **[IN PROGRESS]**

Four searches running, to establish honestly whether MMFDB is novel and to write
a related-work section that survives review:

1. Fluorescence data standards and deposition — FLR/IHMCIF status, Photon-HDF5,
   TTTR vendor formats, FRET community standardisation, and whether any
   fluorescence data repository already exists
2. Bioimaging data management — OME/OMERO/NGFF, IDR, BioImage Archive, REMBI,
   Micro-Meta App, QUAREP-LiMi, and flow cytometry (FCS 3.1 / Gating-ML /
   MIFlowCyt) as a domain that did standardise analysis provenance
3. smFRET and time-resolved analysis software — whether any records provenance
   or exports a community standard
4. General provenance infrastructure — W3C PROV, CWLProv, RO-Crate, and whether
   structural-biology archives capture analysis provenance at all

The reviewer question these must answer: **"why not OMERO?"**

---

## 9. TODO before the paper can be submitted

Ordered by dependency. Items in the same block are independent of each other.

### Block A — unblocks everything else

- [ ] **A1** Install the examples stack: `pip install -e ".[examples]"` (fretbursts and phconvert are missing; tttrlib 0.27.0 and NumPy 1.26.4 are present). Blocks A2, C2
- [ ] **A2** Rewrite notebook 06 standalone (§4.1–4.3) and re-run it. Do not hand-edit numbers
- [ ] **A3** Decide the FLR record's scientific content (§6) — needs your input, informed by the literature survey

### Block B — de-branding, must precede any claim of tool-agnosticism

- [ ] **B1** `queries/artifacts.py:868` — `software_package` default `"chisurf"` → `None`
- [ ] **B2** Check whether `workflow/runner.py::_neutralize_import_software` is then redundant; delete if so
- [ ] **B3** `security/credentials.py:9` — `SERVICE_NAME` → `"MMFDB"`
- [ ] **B4** `config.py:582` — settings dir → `~/.mmfdb` with `~/.chisurf` fallback; update `conftest.py` guards
- [ ] **B5** `repository.py:821` — data block `chisurf_flr_export` → unbranded
- [ ] **B6** Re-run the 758-test suite after B1–B5

### Block C — DEFERRED to the follow-up paper

Not required for this submission. Retained as the agreed next step.

- [ ] ~~**C1** Container executor~~ — deferred (§5)
- [ ] ~~**C2** Worked container example~~ — deferred
- [ ] ~~**C3** Populate a real FLR record~~ — deferred (§6); A3 no longer blocks submission
- [ ] ~~**C4** Documentation for both~~ — deferred

### Block D — dictionary remediation **[now optional for this paper]**

The "intend to propose" claim is softened to future work, so D no longer gates
submission. **But** if the paper features the 229-item fit-parameter registry as
evidence of versatility, the branded category name is exposed to any reviewer who
opens the dictionary. Either do D1–D2, or do not feature the registry.

- [ ] **D1** Rename `flr_chisurf_parameter` (229 item tags, no live table, no data migration — cheap)
- [ ] **D2** Add `_dictionary` header with title, version, datablock id
- [ ] **D3** Register legacy spellings in `EXTENSION_CATEGORY_ALIASES`
- [ ] **D4** Update `_mmfdb_schema` bindings and the vocabulary loader
- [ ] **D5** Decide the prefix policy (§2.5) — deferred with the proposal

### Block E — repository and documentation hygiene, cheap and visible

- [ ] **E1** Delete stray `examples/None`, `None-shm`, `None-wal` and `examples/backups/` (~2.8 MB; reaches the docs tree via the `docs/examples` symlink)
- [ ] **E2** Fix the "executed on every test run" claim in `docs/index.md` and `docs/usage.md` — either add a notebook-execution test or soften the wording
- [ ] **E3** Correct or annotate the FCS notebook's inconsistent concentration figure (~0.03 molecules against a configured population of 1.5)
- [ ] **E4** Add `CITATION.cff`

### Block F — availability, required by the venue

- [ ] **F1** Release to PyPI (currently "once released")
- [ ] **F2** Archive a tagged release and mint a Zenodo DOI
- [ ] **F3** Decide authorship and funding statements

### Block G — optional, decide after the survey

- [ ] **G1** Benchmarks: ingest, provenance traversal, deposit export on a realistic TTTR dataset. Nothing exists today; `.benchmarks/` is empty
- [ ] **G2** Decide whether to fix or disclose the mmCIF importer asymmetry (16 loops exported, 9 consumed)
- [ ] **G3** Consider exposing vocabulary registration over JSON-RPC, so remote clients can extend it too

---

## 10. Literature survey — findings that change the manuscript

Two of five surveys have landed. Bibliography extended to 60 entries, all new
ones retrieved from DOI registrars by content negotiation rather than written
from memory; 4 entries carry `VERIFY` notes (three USENIX papers have no DOI).

### 10.1 ModelCIF is alignment, not a threat **[resolved]**

`_ma_protocol_step` already carries `input_data_group_id`, `output_data_group_id`,
`software_group_id` and `method_type`; `_ma_software_parameter` carries typed
`name`/`value`/`data_type`. An IHM-working-group reviewer would otherwise say
MMFDB reinvented it.

**Scope distinction that resolves it:** ModelCIF describes how a *computed model*
was built, declared at deposition time. MMFDB describes how *experimental data*
were processed, captured at run time.

**Opportunity, verified:** `mmcif_ma.dic` is already bundled and parsed — **56
`ma_` categories** are in the runtime dictionary, including `ma_protocol_step`,
`ma_software_parameter` and `ma_data` — but **zero `ma_` items are bound to any
column**. Reusing the ModelCIF pattern costs no new dependency, only bindings.

### 10.2 The precise novelty statement

Verified by exhaustive grep over the shipped `mmcif_std`, `pdbx_v50`, `ihm_ext`,
`ihm_flr_ext` and `ma` dictionaries — **none of these exist anywhere in the
mmCIF family**: content checksums (zero `md5`/`sha256`/`checksum` items),
wall-clock execution timestamps, `command_line`, execution status, or a
first-class edge table with a relationship type.

Sharpest positioning: *flrCIF models the deposited endpoint; MMFDB models the
whole chain including intermediates that never reach the deposit, captured at run
time rather than annotated at deposition time.*

### 10.3 Claims that must be corrected

| Claim as drafted | Correction |
| --- | --- |
| "a GUI click" as the unorchestrated example | **Self-inflicted.** In Galaxy a GUI click *is* orchestrated. Use "outside the system's execution boundary" and pick a desktop application analysing in-process |
| Workflow engines cannot record unorchestrated runs | **True and now provable from primary source:** Galaxy's provenance controller implements `index` (GET) only — `create` and `show` raise `HTTPNotImplemented()`, `delete` raises `HTTPBadRequest` |
| Content addressing as a contribution | **Established practice** — OCFL, DataLad, Software Heritage, Guix. Cite, do not claim. The novelty is that no mmCIF dictionary content-addresses |
| Structural biology deposits only results | **False.** ModelCIF, PDB-IHM and PRIDE all capture some provenance. Defensible version: unusual as a *queryable cross-entry database of runs* rather than per-entry deposition annotation |
| Single self-contained standards file is novel | **Partially.** Prior art: mzML (base64 spectra + `dataProcessingList`), NeXus (`NXprocess`), COMBINE/OMEX ("one file to share all information"). What survives: none encodes a general typed DAG |

### 10.4 Citations that cannot be omitted

- **AiiDA** (`aiida2016`, `aiida2020`) — production materials-science database whose core abstraction is a typed Data/Process provenance DAG. Closest architectural analogue anywhere, and it cites W3C PROV nowhere
- **BURRITO** (`burrito2012`) — passive capture plus human annotation; nearest published ancestor
- **Process/Provenance Run Crate** (`leo2024`) — already standardises manually-executed steps in near-identical language. **Claim compatibility**, turning the closest competitor into an interoperability selling point
- **ModelCIF** (`modelcif2023`) and **PDB-IHM** (`pdbihm2025`)

### 10.5 PROV: align, do not adopt

Every verified adopter uses PROV for export, not storage. Recommendation: a thin
PROV-O export view over the native typed model (`artifact → prov:Entity`,
`operation → prov:Activity`, edges → `used`/`wasGeneratedBy`). One serializer.
The honest caveat worth a sentence: PROV-CONSTRAINTS validity checking
(cycle-freedom, temporal consistency) has no equivalent in a hand-rolled model.
Cite `moreau2015` for the rationale, not the EDBT tutorial.

### 10.6 The FAIR hook **[new, from your framing]**

FAIR principle **R1.2** is "(meta)data are associated with detailed provenance"
(`wilkinson2016`, currently uncited). For fluorescence this makes workflow
mapping a standards obligation rather than good practice: reusing a FRET distance
requires knowing which correction factors, burst criteria and fit produced it.

### 10.7 "Why not OMERO?" — answered **[resolved]**

The bioimaging survey settles the question a reviewer was always going to ask.
The answer is structural, not a matter of missing fields.

**OME cannot represent a photon record.** Its temporal model is frame-level —
`Plane/@DeltaT` — with no macrotime or microtime concept anywhere, so a TTTR
record has nowhere to live. The only pulsed-excitation hooks in the entire model
are `Laser`'s `Pulse`, `RepetitionRate` and `FrequencyMultiplication`: no pulse
width, no sync-out phase, no PIE interleave delay. This is a cleaner argument
than enumerating absent TCSPC fields.

**REMBI concedes the gap itself.** Its whole `ImageAcquisition` module is three
fields, of which `image_acquisition_parameters` is free description — every
TCSPC, FCS and smFRET parameter collapses into one string. The paper states
*"it is currently difficult to expand the recommended metadata required for
archival deposition beyond the basic information needed to open a dataset and
access the pixel data"* (`sarkans2021`). The authors of the standard explain why
it does not cover this domain.

**Zero coverage is measured, not asserted.** Full-text scans of `hammer2021`, the
two QUAREP-LiMi founding papers, and a code search over the live
4DN-BINA-OME spec repository return **0 hits** for TCSPC, FLIM, lifetime,
correlator, photon counting, instrument response, gamma factor and crosstalk —
with control terms (Objective, Detector, Tier) confirming the index is live.

**The two communities are disjoint.** `lerner2021`, the smFRET community's own
open-science and reporting appeal, is not referenced by or harmonised against
REMBI, OME, 4DN-BINA-OME or QUAREP. The mutual non-citation is itself evidence
for the niche.

**Two things to concede gracefully** — conceding them makes the rest land harder:

1. `<AdditionalDimensionMap>` in `hammer2021` is explicitly motivated to handle
   "fluorescence lifetime, polarization angle, and lambda" beyond the five
   canonical dimensions. It is a *dimension label* with no parameters hanging off
   it, but a reviewer who knows the 4DN work will raise it.
2. QUAREP-LiMi WG7's deliverable is now the LiMi-Model on a LinkML foundation
   (`limimodel2024`), which makes a formal time-resolved extension technically
   feasible for the first time. Frame as convergent, not competing.

**Cite the author corrections alongside the originals**: `hammer2021corr`,
`rigano2021corr`, `boehm2021corr`.

**Must verify before submission** (the survey could not): QUAREP **WG15 (FLIM)**
scope, chairs and deliverables — the closest live overlap with any FLIM claim,
and the page timed out repeatedly. Also `monterollopis2026`'s field list
(publisher returned 403) before asserting it is intensity-imaging only.

### 10.8 CORRECTION — Photon-HDF5 *does* have provenance **[must not get this wrong]**

The draft must not say Photon-HDF5 lacks provenance. It has a `/provenance`
group. Being imprecise here is the fastest way to lose a reviewer who knows the
format.

**What it actually is:** six flat fields — `filename`, `filename_full`,
`creation_time`, `modification_time`, `software`, `software_version` — all
describing *the original vendor file before conversion*. It is a file-conversion
receipt: one hop, optional (absent when written directly), non-recursive, and
about the **acquisition** software only. There is no field for any analysis tool,
no checksum (phconvert issue #11, open since 2016), and no link to any other file.

**The precise claim:** Photon-HDF5 records where a file *came from*; it does not
record what was *done to it*.

**The strongest supporting evidence is the maintainers' own design decision.**
Issue #38 "Correction factors" (opened 2016-11-17, still open) asks for donor
leakage, direct excitation, γ and β. The reply:

> "In general I see the Photon-HDF5 file as *immutable raw data*. Any
> post-analysis data should go in a different file."

A follow-up in 2017 proposed adding them; the thread died in November 2017 and
nothing was implemented. So there is still nowhere in a Photon-HDF5 file for γ,
β, leakage or direct excitation, nor for the calibration that produced them.

**The data-model argument, which is the one to make in the paper:** γ depends on
*both* setup and sample, so it fits neither `/setup` nor `/sample`. A flat
five-group schema structurally cannot represent a quantity that is a function of
a (setup, sample, session) tuple. That is precisely what a relational schema plus
a typed edge is for — and it connects directly to MMFDB's `calibrated_by` edge.

### 10.9 The gap statement — the Introduction's backbone

Three layers. Fluorescence spectroscopy has none of them.

1. **No minimum-information checklist.** Nothing in FAIRsharing for smFRET, FCS,
   TCSPC or FLIM.
2. **No exchange-format standard.** JCAMP-DX halted development in 2006 with
   seven techniques, none of them fluorescence; AnIML has no fluorescence
   definition and none planned; NeXus offers only a generic
   `NXoptical_spectroscopy` contribution with no FLIM/TCSPC/FCS/smFRET
   application definition.
3. **No repository records a computational provenance DAG** — not PDB-IHM,
   EMPIAR, SASBDB, PRIDE, IDR or BioImage Archive. All record inputs and outputs
   with flat method metadata; none stores the edges.

**The comparison that makes it land: flow cytometry solved all three** —
MIFlowCyt (`miflowcyt2008`) as checklist, FCS 3.2 (`fcs32`) as format, Gating-ML
(`gatingml2015`) as a machine-actionable analysis description. Same photons,
same detectors, complete stack. Fluorescence spectroscopy has checklist-free,
format-free, analysis-description-free practice.

**Quotable, verified facts:**

- Hellenkamp (536 citations) and Lerner (321) versus Photon-HDF5 (68, OpenAlex —
  note Semantic Scholar says 50 and Europe PMC 44; cite one source and say
  which). **The community agreed on how to do the experiment and never agreed on
  how to store it.**
- Photon-HDF5 citations are flat at ~6/year for a decade — no adoption curve.
- The spec has been at tag `0.5rc1` since 2017; it is the only tag in the repo.
- `agam2023`, the field's flagship 19-laboratory benchmark, was deposited ad hoc
  on Zenodo because **no domain repository exists to deposit into**.
- Bio-Formats has **no PicoQuant PTU reader at all** — the dominant format in the
  field is invisible to the dominant bioimaging library.
- OME-NGFF **RFC-7 "Channel provenance" is listed date TBD, status TBD**.
- SMD (`smd2015`) was a prior attempt at a single-molecule data format: 13
  citations in 11 years. The field has tried this once and it did not take.
- Vendor lock-in has three flavours: PicoQuant's format is documented only by an
  unlicensed excerpt of a commercial manual in a repository with **no LICENSE
  file**; Becker & Hickl's specification is explicitly **not redistributable**
  (Bio-Formats' own words), so every open reader is reverse-engineered; Swabian
  publishes nothing.

### 10.10 Citation hygiene

The bibliography is now **98 entries**, all fetched from DOI registrars by
content negotiation. **One reported DOI resolved to the wrong paper** —
`10.1093/nar/gkac1006`, given for BMRB, returns "TIMEDB: tumor immune
micro-environment cell composition database". That entry was dropped; re-verify
BMRB before citing it. Treat every survey-reported DOI as unverified until it
resolves to the expected title.

### 10.11 The novelty claim, narrowed **[supersedes §1 wording]**

The smFRET-software survey found two systems that come close enough to force a
rewrite. Both must be cited and distinguished; neither is currently in the draft.

**Threat 1 — OME had this in 2005.** Goldberg *et al.* (`goldberg2005`) used the
phrase "data provenance for biological microscopy" verbatim, with "strong
semantic typing" governing which analysis modules could chain, stored in a
database, with per-execution `ModuleExecution` records. That is a typed,
chained, database-backed analysis provenance graph, twenty years ago.
**Defence:** OME could only record modules registered *inside* it and executed
*by* its AnalysisEngine. It could not describe an analysis someone ran in MATLAB
on their laptop. Cite it as the ancestor and claim the generalisation, not the
concept.

**Threat 2 — BIOMERO 2.0, published three months ago** (`luik2026`; predecessor
`luik2024`). Its abstract states: "All imports and analyses are recorded with
parameters, versions, and results, ensuring real-time provenance."
**Defence:** it records only containerised workflows *it dispatched* to Slurm —
an orchestrator's execution log, not a description of foreign tool runs; its
typing is workflow-shaped (`WorkflowRun`→`Task`), not domain-shaped; it has no
archival export; and it is pixel bioimaging, with nothing about photon streams,
lifetimes or bursts. **Get the PDF before writing the distinction — the data
model is paywalled and unverified.**

**The narrowest defensible formulation.** Every clause is load-bearing:

> No existing system records, as a typed and queryable graph, which **external**
> tool at which version with which parameters produced a given derived
> **fluorescence** observable, and exports that record as a **wwPDB-archivable
> mmCIF deposition**.

Drop "external" and OME 2005 takes it. Drop "typed graph / domain schema" and
BIOMERO takes it. Drop "mmCIF export" and generic PROV/RO-Crate tooling takes it.

### 10.12 The best motivation available — use this

**Empirical, from all 392 PDB-IHM entries.** Only **seven** contain FLR content.
Of those seven: **two** have a null `software_id`; **four** point at a *modelling*
program (Olga, FPS, AvTraj) — the tool that built the structure, not the one that
produced the FRET observable; and **one** (8ZZJ) points at a genuine analysis
program (Tatiana, PDA) **with no version recorded**.

**The analysis-software link is never populated with a versioned analysis tool.**
And the real deposition interface today is `flr2mmcif` — **a hand-filled Excel
template**. That is the friction point, and it is exactly where MMFDB sits.

**Two supporting results:**

- `agam2023`: one person reanalysing the *same* data improved inter-laboratory
  agreement from s.d. ±0.03 to **±0.008 with no change in the mean** — roughly a
  fourfold reduction from standardising the analysis alone, with γ the limiting
  term at Δγ/γ ≈ 23 %.
- `goetz2022` (kinSoftChallenge, 11 tools, 14 analyses, 12 laboratories, blind):
  rate-constant CV rose from 12–16 % on easy data to **33–45 %** on hard data;
  reported uncertainties spanned 0.4 %–21 % because "no common standard exists";
  only 4 of 14 recovered the correct model under kinetic heterogeneity. Sharpest
  detail: **the benchmark could only be conducted because the organisers manually
  collected a version string for each analysis — none of the tools emitted that
  metadata natively.**

### 10.13 Honesty items about our own software

- **ChiSurf has a provenance defect.** `chisurf/info.py:7` computes
  `__version__` from `date.today()` at import, so the `chisurf_version` written
  into `project.yaml` records **the date the software was run, not the build**.
  Two builds run the same day are indistinguishable. Do not cite ChiSurf's
  session file as a provenance record without this caveat.
- **Do not let a reviewer misread tttrlib.** Its paper says it "allows
  conveniently creating and archiving settings used in analysis in nonrelational
  object databases or dictionary files (e.g. JSON, YAML, or mmCIF)". That is an
  **affordance, not an implemented feature** — tttrlib writes no such record,
  defines no schema, binds nothing to a result and checksums no inputs.
- **Cite phconvert positively.** It auto-populates `/identity` and writes
  `/provenance` against a field specification: conversion provenance done well
  for one bounded transformation. Conceding this strengthens the argument — the
  community can do this when the transformation is well defined, and simply never
  extended it upward.

### 10.14 The one genuinely unoccupied claim

**No tool surveyed content-hashes its inputs.** Zero hash or checksum hits across
Mars, PAM, FRETBursts and ChiSurf; and IHMCIF's `ihm_external_files` carries
path, size and format but **no hash field**. Combined with the earlier finding
that no mmCIF dictionary anywhere contains a checksum item, this is the clearest
piece of unoccupied ground in the whole survey.

### 10.15 Corrections to make in the draft

| Item | Correction |
| --- | --- |
| Introduction names only FRETBursts, PAM, ChiSurf, tttrlib | Acknowledge the TIRF side (`spartan2016`, `mashfret2018`, `deepfret2020`, `ebfret2014`) and the FLIM side (`flimfit2013`, `flimj2020`, `flute2023`, `alligator2025`) or reviewers from those communities will object |
| Mars as a burst tool | It is **not** — it is Fiji/ImageJ2 for single-molecule properties from bioimages. Cite as a neighbouring-field provenance benchmark |
| "PDB-Dev" | Unified with the PDB in August 2024 and renamed **PDB-IHM** (`pdbihm2025`) |
| Notebooks as provenance | Worth stating explicitly: a notebook records what code was *typed*, not what was *executed against which bytes*. Notebooks give re-executability, not provenance |

**Do not cite (unverified):** Fretty; PARIS/Margarita/Kristine/Tatiana as named
citable tools; FPS's file format; SPARTAN's `fileMetadata` contents; iSMS
provenance; BIOMERO 2.0's data model.

### 10.17 Claims in the draft that are REFUTED **[act on these first]**

The cross-domain survey is the most damaging of the five. Four sentences
currently in `mmfdb.md` do not survive contact with prior art.

**The worst of it: `merkys2017` — AiiDA's TCOD exporter, 2017, in the CIF
family.** It writes `_tcod_computation_command`, `_tcod_computation_stdout`
/`_stderr`, `_tcod_computation_wallclock_time`, `_tcod_software_package_version`,
and `_tcod_file_contents` **with `_tcod_file_md5sum` and `_tcod_file_sha1sum`**,
into one CIF declaring `_audit_conform_dict_*`. Combined with AiiDA's SHA-256
content-addressed store with dedup and its typed DAG carrying
`ProcessNode.exit_status`, that is essentially our abstract's sentence,
implemented eight years ago. **The earlier "no mmCIF dictionary carries a
checksum" finding must be scoped to *PDBx/mmCIF*, not the CIF family.**

| Sentence in the draft | Why it fails | Fix |
| --- | --- | --- |
| "The rendered invocation and the process exit status are columns of the operation table rather than entries in a free-form settings blob…" — one of "three design choices that matter" | Galaxy's `job` table has carried `tool_id`, `tool_version`, `galaxy_version`, `command_line`, `exit_code`, `state`, stdout/stderr and timestamps **as columns since 2010** (`goecks2010`), with a separate `JobParameter` table. We already cite Galaxy | **Cut it as a design claim.** Reframe around the closed vocabulary and the calibration edges |
| "None answers what a reviewer actually asks: which photons, which tool, which version, which parameters." | Mars (`huisjes2022`) answers three of four; Galaxy answers all four | Narrow to *typed and queryable rather than logged as prose* |
| "Raw bytes live in a content-addressed object store keyed by MD5… verified by SHA-256" | DVC documents `md5/{2-char}/{rest}` with dedup verbatim; AiiDA uses SHA-256 with dedup | Cite both; claim only the **dual-digest discipline** (routing digest never load-bearing for integrity) |
| "a single self-describing mmCIF file… every data file base64-embedded" | True for **PDBx/**mmCIF; false for the CIF family — TCOD did it in 2017 | Add "PDBx/" and cite `merkys2017` |
| artifact / operation / edge presented as a design choice | ≡ PROV Entity/Activity/relation, and ≡ RELION's `pipeline_nodes`/`_processes`/`_input_edges`/`_output_edges` since 2016 | Cite `provdm2013` **and** `provo2013` in §2.1 and state the mapping explicitly. A reviewer who sees the mapping cannot accuse us of ignorance |

**A process problem worth naming.** The bibliography now holds 125 entries; the
manuscript cites 21. Several works sitting uncited in our own `.bib`
(`huisjes2022`, `aiida2016`, `leo2024`, `provdm2013`) directly contradict
exclusivity claims in the Introduction. In review that reads worse than not
knowing them.

### 10.18 What survives — and it is enough

Four things hold up across all five surveys:

1. **Base64-embedded payload in a single PDBx/mmCIF file.** RO-Crate has **zero**
   occurrences of "base64" across its specifications; its ceiling is a zipped
   directory tree whose manifest is explicitly "not necessarily an exhaustive
   manifest or inventory". `_ihm_external_files` references only. Scope the claim
   to PDBx/mmCIF and cite `merkys2017` for the CIF-family precedent.
2. **An integer exit code in a deposition.** Workflow Run Crate issue #15
   **decided** to collapse exit status to
   `CompletedActionStatus`/`FailedActionStatus`; there is no portable RO-Crate
   representation. This is a *decided design position*, not an oversight — the
   strongest single differentiator found in the entire survey.
3. **A closed, dictionary-declared predicate vocabulary** (35 operation types, 12
   relationship types). Galaxy has no predicate column anywhere in 222 model
   classes; NWB's schema language **cannot express enumerations at all** (open
   issue #640); SciCat's `usedSoftware` is a bare URL string and its
   `jobParameters` untyped.
4. **`calibrated_by` and the correction-notice story — not prior art anywhere, in
   any cluster, in any domain.** This is the best material in the paper and it is
   currently **one sentence in §2.1**. Promote it.

The four-way combination — content-addressed store, run-time typed operation node
carrying an exit code, dictionary-generated schema, and the DAG as native mmCIF
categories — is unmatched. A Europe PMC sweep of FCS/FLIM/TCSPC/single-molecule
against data-management/repository/provenance returned nothing but Mars. **The
domain-level claim is safe; the general claims are not.**

### 10.19 Two framing gifts

**"Why not OMERO?" now has a one-fetch answer.** `openmicroscopy.org/Schemas/`
still lists under *Legacy Schemas*: `AnalysisModule` ("image analysis
algorithms… how information is generated in OME"), `AnalysisChain` ("how module
outputs are connected to inputs of other modules") and `DataHistory` ("describes
the data dependency in OME"). The current `ome.xsd` (261 kB) contains
`provenance` 0 times, `workflow` 0, `pipeline` 0, `software` 1. **OME built
analysis provenance and abandoned it in 2009.** That is verifiable and far
stronger than asserting a gap.

**CORRECTED — do not repeat the "both are dead" claim.** The survey reported that
Renku and Pachyderm both solved content-addressed storage plus a PROV DAG and
that both are dead. Checked directly on 2026-07-29:

| System | Verified status |
| --- | --- |
| `renkulab.io` | HTTP 200 — live |
| `SwissDataScienceCenter/renku` | 272 stars, **not** archived, last push **2026-07-29** |
| `SwissDataScienceCenter/renku-python` | last push 2025-04-15 |
| `pachyderm.com` | DNS **SERVFAIL**, unreachable |
| `pachyderm/pachyderm` | 6,299 stars, **not** archived, last push **2025-02-03** |

**Renku is actively developed** — "dead" is simply wrong. Pachyderm's domain no
longer resolves and its open-source repository has been quiet for ~18 months, but
it is not archived, so "dead" overstates it too. Say only what is verified: *the
Pachyderm open-source project has been quiet since February 2025 and its domain
no longer resolves.*

The sustainability argument does not depend on either project and survives intact
on evidence that **is** verified: `kinsoftchallenge.com` is NXDOMAIN, the FRET
community bylaws remain a January 2020 draft, the Photon-HDF5 site repository has
not moved since 2017, and flrCIF has no license and no releases. The argument to
make is therefore **not** novelty but a light, domain-scoped, four-dependency
design that a laboratory can actually keep running.

*What these systems are, if they are cited:* Pachyderm is a Kubernetes-based data
versioning and pipeline platform — content-addressed, deduplicated data repos
with Git-like commits, pipelines declared as containers triggered by new input
commits, and lineage recorded automatically. It is the closest precedent for the
deferred `container:` executor (§5). Renku (Swiss Data Science Center) combines
Git/Git-LFS versioning, Docker environments, and a PROV-style knowledge graph,
with `renku run` wrapping an arbitrary command and recording that execution —
conceptually close to our `external_tool` operation.

Minor: Taverna retired from the Apache **Incubator** on 2020-02-20 — it never
graduated to a top-level project and so never reached the Attic. Write
"Incubator".

**Do not assert (unverifiable):** a peer-reviewed CCP-EM pipeliner/Doppio paper;
KNIME provenance-export features; what OMERO.mde does (paywalled); REMBI's
analysis field names (help page 404s); autoPROC licence terms.

### 10.21 The measured evidence — strongest material in the paper

All five surveys are in. The fluorescence-standards survey ran a full scan of the
PDB-IHM archive; **I re-ran and independently verified it.** Artifacts are
preserved in `paper/evidence/` (`scan.py`, `scan_out.json`, `holdings.json`,
`cfh.json`) — re-run before submission to refresh.

**Verified counts: 392 released PDB-IHM entries scanned; 7 contain any flrCIF
category (1.8 %)** — 8ZZJ, 9A08, 9A1A, 9A1G, 9A2E, 9A2F, 9AAO. Sixteen categories
appear in all seven; **`flr_kinetic_rate_analysis` and
`flr_relaxation_time_analysis` — the headline of Hanke *et al.* 2024 — appear in
exactly one entry each.**

**"Why not just extend flrCIF?" now has a measured answer.** flrCIF is at v0.03;
its last *substantive* change was v0.009 in December 2022 (subsequent revisions
are parent-dictionary bumps); four category groups remain in `in_development`;
the repository has 27 commits, 0 releases, 0 open issues, 3 stars and **no
LICENSE file**; and there is **no documented process for proposing an extension**
(no CONTRIBUTING.md in either flrCIF or IHMCIF — both 404). Decisive detail:
PDB-IHM's own DERIVA/ERMrest catalog exposes 117 tables including
`ihm_probe_list` and `ihm_kinetic_rate`, but **not a single `flr_*` table**.
flrCIF content sits inside the mmCIF files and is invisible to the archive's own
query layer.

**Position MMFDB as the system that feeds flrCIF, not one that competes with it.**
Raw data is never in the archive — all seven entries point to Zenodo or a
supplementary spreadsheet, and no TTTR photon stream has ever been deposited.

**Four independent negative searches, all from primary sources:**

- **re3data** registers **zero** repositories for fluorescence spectroscopy;
  "fluorescence lifetime" returns 0 hits.
- **Europe PMC**: `TITLE:"FRET" AND TITLE:"database"` → **0 hits**.
  `TITLE:"provenance" AND TITLE:"spectroscopy"` → 20 hits, **every one about
  geographic provenance** (honey, olive oil, timber). Computational provenance is
  not a concept in this literature.
- **Zenodo**: records matching smFRET total **78, ever** — the field's entire
  structured deposition footprint, as unindexed blobs.
- **Nature Portfolio's recommended-repository table contains no fluorescence
  repository at all.**

**The deposition that best illustrates the problem:** `agam2023`'s data is 35
files and 29.5 GB of one opaque ZIP per laboratory per protein, with inconsistent
naming (`Lab4 MalE.zip` beside `Lab4_U2AF2.zip`), and a code-availability
statement that is a prose list of tool names with no versions and no parameters.
The word "checklist" appears zero times in the article.

### 10.22 The real exposure is adoption, not novelty

Novelty holds. What a reviewer will actually press on is why MMFDB will not decay
like everything before it:

- `kinsoftchallenge.com` is **dead** (NXDOMAIN, nothing in the Wayback CDX) four
  years after publication.
- The FRET community's bylaws are **still a draft from January 2020**, with
  unanswered comments.
- The Photon-HDF5 website repository has not moved since **2017**; the spec has
  been at `0.5rc1` since then.
- flrCIF has 3 stars and no license.
- Pachyderm solved content-addressed storage plus automatic lineage; its
  open-source repository has been quiet since **February 2025** and
  `pachyderm.com` no longer resolves. (**Do not** extend this to Renku — Renku is
  actively developed; see the corrected table in §10.19.)

**Pre-empt it.** Argue for a light, domain-scoped, four-dependency design a
laboratory can keep running — not for novelty. Then commit to governance: a
license, versioned releases, a citable DOI, and a named venue. Two are in window:
**MAF2026 Copenhagen** (with a FLIM data-analysis workshop) and the BPS thematic
meeting **"Single-Molecule FRET: The Next 30 Years", Tutzing, 13–17 September
2026**. Note the author sits on the FRET community advisory board, which makes
this credible rather than aspirational.

### 10.23 One more comparator to cite and differentiate

**FPbase** (`lambert2019fpbase`, 1042 proteins via its live API) is the closest
analogue to MMFDB's optical-components surface. Cite it and state what is added:
organic dyes, filters, dichroics, detectors and light sources, with spectra,
approval workflow and duplicate merging. **Mars** (`huisjes2022`) remains the most
serious single-molecule comparator — but it is bioimage/TIRF-shaped, with no TTTR
stream, no γ/β or IRF semantics, no server, and no mmCIF export.

### 10.24 Survey status and unverified claims

**All five surveys have landed** — fluorescence data standards, bioimaging data
management, the smFRET software landscape, generic provenance infrastructure, and
cross-domain platforms. Bibliography: **128 entries**, every one resolved against
a DOI registrar and title-checked.

**Claims that must be verified before they appear in the manuscript.** These came
from surveys and have *not* been confirmed:

- That **Renku 2.0 removed the knowledge graph** carrying its provenance.
- That **HPE acquired Pachyderm** (recollection, not checked) — the likely reason
  `pachyderm.com` no longer resolves, but do not assert it.
- **QUAREP-LiMi WG15 (FLIM)** scope, chairs and deliverables — the closest live
  overlap with any FLIM claim; the page timed out repeatedly.
- **BIOMERO 2.0's data model** (`luik2026`) — paywalled, and we distinguish MMFDB
  from it in §10.11. Get the PDF.
- `monterollopis2026`'s field list (publisher returned 403).
- Whether OME-NGFF has any FLIM/TCSPC RFC in flight beyond RFC-3.

**One DOI already caught mis-resolving:** `10.1093/nar/gkac1006`, reported for
BMRB, returns an unrelated paper. Treat every survey-reported DOI as unverified
until it resolves to the expected title.

**Reproducible evidence** is preserved in `paper/evidence/` — `scan.py` and
`scan_out.json` for the 392-entry PDB-IHM sweep (re-verified: 7 entries, 1.8 %).
Re-run before submission.

---

## 11. The full DAG: excitation source → detector → photon → result **[NEW — outline]**

The DAG MMFDB defines should span the whole chain, not just the analysis half:
from the excitation source through the optical path to the detector, and from the
photon record through each analysis step to the reported observable (an FCS
correlation curve, a TCSPC decay, a PCH histogram, a burst table, a FRET
efficiency, a distance restraint).

### 11.1 Assertion: what is actually in `src` today

Checked directly. The result is more interesting than a simple yes.

| Layer | Status in `src` |
| --- | --- |
| **Analysis DAG** (photon → result) | **Present but DERIVED, not stored.** `input_to` and `produced` are synthesized on read in `provenance/graph.py` from `mmfdb_operation_artifact` rows (`direction` = input/output). They are never materialized |
| **`mmfdb_edge`** | **Explicitly forbids the DAG's primary edges** — `CHECK (relationship_type NOT IN ('input_to','produced'))` (`schema/schema.py:931`); `add_edge` raises "Operation input/output links must use record_operation_link" (`queries/artifacts.py:1642`). It carries the 12 *non-execution* relationships only |
| **Optical DAG** (source → detector) | **Absent.** `mmfdb_setup` holds optics as JSON blobs — `configuration_json`, `detectors_json`, `irf_definition_json`, `timing_calibration_json`, `dark_count_json`. `flr_instrument` is 6 columns with free-text `details`. Zero hits for `optical_path`/`light_path`/`beam_path`/`path_order`/`component_order` anywhere in `src` |
| **Optical components** | Catalog exists — 5 types (fluorophore, filter, dichroic, detector, light source) with 7 spectrum types — but it is a **parts bin with no assembly**. Nothing connects a filter to a dichroic to a detector |

**Consequence for the manuscript.** §2.1 currently says "Directed edges connect
them" and "a separate table carries twelve non-execution relationships". That is
true but misleading: it implies the DAG lives in the edge table when the edge
table is precisely where it is forbidden. Either state that the execution edges
are a typed projection of the ports table (defensible — it is a normalisation
choice that prevents the dual-write disagreement the code comments call out), or
do not describe an edge table at all. **Do not let a reviewer discover this by
reading the schema.**

### 11.2 The three-layer DAG to define

```
EXCITATION      light source → excitation filter → dichroic → objective
                                                                  ↓
SAMPLE                                                         sample
                                                                  ↓
EMISSION        objective → dichroic → emission filter → detector channel
                                                                  ↓
MEASUREMENT                                          photon record (TTTR)
                                                                  ↓
ANALYSIS        operation → derived artifact → operation → observable
                (burst search, correlation, decay fit, PCH, …)
```

Only the ANALYSIS layer exists today, and only as a derived view.

### 11.3 Why this is the paper's argument, not a feature request

This closes the gap identified in §10.8. Photon-HDF5's maintainers could not
place γ because **γ is a function of a (setup, sample, session) tuple** and a flat
five-group schema cannot express that. A DAG spanning optics, sample and analysis
can — and MMFDB already holds the ingredients:

- γ depends on the **detection-efficiency ratio** (emission filters, dichroic,
  detector quantum efficiency) and on donor/acceptor **quantum yields**.
- **Crosstalk** depends on the donor emission spectrum overlapping the acceptor
  detection band.
- **Direct excitation** depends on acceptor absorption at the donor excitation
  wavelength.

Every one of those is a spectral overlap over components **already curated in the
optical-components catalog**. Making the optical path a graph turns the
correction factors from magic numbers into *derived, traceable quantities whose
inputs are themselves registered artifacts* — and `calibrated_by` (§10.18, item
4, the one thing that is prior art nowhere) then reaches all the way back to the
physical components, not just to a calibration measurement.

That is the strongest version of the paper's claim: **not "we record which tool
ran", but "we record the whole causal chain from photon generation to published
observable, in one typed graph."** No system found in five surveys does this —
flrCIF's `flr_instrument` is free text, OME models frame-level pixels, and
Photon-HDF5 declined the problem explicitly.

### 11.4 Outline implications

- **Figure 1b** should show the three layers, not just artifact → operation →
  artifact. The excitation-to-detector half is currently invisible in the figure.
- **§2.1** gains the layered DAG as the data model, with the honest note from
  §11.1 about execution edges being a projection.
- **Results** should show one observable — an FCS correlation curve or a PCH
  histogram — traced end to end, since those are the cases where the instrument
  parameters (`mmfdb_setup_fcs_pair` already has `n_casc`, `n_bins`, channel
  pairs) genuinely determine the result.
- **Scope decision needed:** the optical-path graph does not exist in `src`. It is
  either (a) described as the model MMFDB defines with the instrument layer
  flagged as not yet implemented, or (b) deferred with the paper claiming only
  the analysis layer. Option (a) risks the "described but absent" objection
  already raised about containers in §5. **[OPEN — needs a decision]**

## 12. Still to grill **[OPEN]**

- Authorship and funding — currently single-author with `[TODO]` placeholders
- Availability — not on PyPI; no Zenodo DOI; no `CITATION.cff`. *Bioinformatics*
  requires a stable availability statement
- Benchmarks — no performance or scalability numbers exist anywhere;
  `.benchmarks/` is empty
- Repository hygiene — stray `None`, `None-shm`, `None-wal` SQLite files
  (~2.8 MB) in `examples/`, plus untracked `examples/backups/`; `docs/examples`
  symlinks to `examples/`, so these reach the documentation tree
- Documentation over-claim — `docs/index.md` and `docs/usage.md` state the
  notebooks are "executed on every test run"; no test in this repository
  executes a notebook
- Whether the mmCIF importer asymmetry (16 loops exported, 9 consumed) should be
  fixed or disclosed
