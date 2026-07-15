# MMFDB — the multimodal fluorescence database

MMFDB stores fluorescence measurements, the analyses run on them, and the
**provenance** linking the two, so a result can always be traced back to the raw
photons and the exact tool that produced it. It is a standalone package usable
embedded or as an HTTP/JSON-RPC service, independent of any analysis application.

New to MMFDB? Read {doc}`concepts` for the data model, then {doc}`installation` to
set it up. To run the worked analyses, see {doc}`usage`; to drive a server or
build a client in any language, see {doc}`rpc-clients`.

## What it provides

- **One store for many modalities** — burst selection, BVA, H2MM, FCS, and
  confocal imaging all work on data pulled from the same object store.
- **Known-truth simulations** — generate photons with a defined answer to
  validate an analysis before trusting it on real data.
- **Provenance for every tool** — any tool (a CLI program, a third-party package
  such as FRETBursts, your own script) records what it did as a queryable lineage
  graph, with no lock-in to a particular framework.
- **Portable deposition** — export a dataset as a self-contained ZIP (native raw
  files + database snapshot + provenance + mmCIF metadata) that another MMFDB
  instance re-imports, or that you upload to Zenodo/OSF with their own tools.

## Contents

```{toctree}
:maxdepth: 1

installation
concepts
usage
rpc-clients
webadmin
```

## The example notebooks

Every workflow is a runnable, **tested** notebook under `examples/` (executed on
every test run, so its rendered outputs are real). Browse the gallery:

```{toctree}
:caption: Example notebooks
:glob:

examples/mmfdb_*
```
