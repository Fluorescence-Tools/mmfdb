"""The provenance graph of a ``.pto``, its mmCIF form, and the way back into MMFDB.

One neutral graph (operations, artifacts, edges, parameters) is the meeting
point of three things that must agree:

* a ``.pto`` container, either a *measurement* (PTO.MFDB: every derived object
  carries its operation, settings and parents as tags) or a *project*
  (``ChiSurf.Project``, decomposed by :func:`mmfdb.project.project_archiver`),
* the MMFDB tables, and
* the ``_mmfdb_provenance_*`` categories of ``mmfdb_workflow_ext.dic``, which
  are what a deposited mmCIF file carries.

``graph_from_pto`` -> :func:`write_graph_cif` -> :func:`read_graph_cif` ->
:func:`import_graph` -> :func:`graph_from_db` must return the graph it started
from; ``tests/test_pto_cif_roundtrip.py`` holds that line.

What travels is provenance, not state: the exact validated project snapshot and
the bytes of every object stay in the container and remain the only restore
authority.
"""

from __future__ import annotations

import hashlib
import inspect
import io
import json
import tempfile
import uuid
import warnings
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from mmfdb.cif_writer import CifWriter

Graph = dict[str, Any]

#: Block name and conformance entry written into every graph CIF.
WORKFLOW_DICTIONARY = "mmfdb_workflow_ext.dic"

_OPERATION_ITEMS = (
    "operation_type", "algorithm", "software_package", "software_version",
    "status", "settings_hash",
)
_ARTIFACT_ITEMS = ("artifact_kind", "data_format", "row_grain", "checksum", "checksum_algorithm")
_EDGE_ITEMS = (
    "source_node_type", "source_node_id", "target_node_type", "target_node_id",
    "relationship_type", "operation_id", "role", "source_row_column", "target_row_column",
)
_PARAMETER_ITEMS = (
    "parameter_id", "operation_id", "name", "value", "standard_error", "initial_value",
    "lower_bound", "upper_bound", "bounds_on", "units", "parameter_type", "role",
)
_FLOAT_PARAMETER_ITEMS = ("value", "standard_error", "initial_value", "lower_bound", "upper_bound")

#: Edge types that are operation-artifact links, not ``mmfdb_edge`` rows.
_LINK_EDGES = {"input_to": "input", "produced": "output"}


def _clean(value: Any) -> Any:
    """Return None for the empty values every source spells differently."""
    return None if value is None or value == "" else value


def canonical(graph: Graph) -> Graph:
    """Return *graph* sorted, so two graphs of the same content compare equal.

    Parameters
    ----------
    graph : dict
        ``{"operations": {id: {...}}, "artifacts": {id: {...}}, "edges": [...],
        "parameters": [...]}``.

    Returns
    -------
    dict
        The same content in a stable order.
    """
    def key(row: dict) -> str:
        return json.dumps(row, sort_keys=True, default=str)

    return {
        "operations": dict(sorted(graph["operations"].items())),
        "artifacts": dict(sorted(graph["artifacts"].items())),
        "edges": sorted(graph["edges"], key=key),
        "parameters": sorted(graph["parameters"], key=key),
    }


#: Artifact kinds that are the primary data: what a measurement recorded, as opposed
#: to anything computed from it.
PRIMARY_KINDS = ("tttr_photon_stream", "trace_data", "image_data", "raw_measurement")


def untraceable(graph: Graph, primary_kinds: tuple[str, ...] = PRIMARY_KINDS) -> list[str]:
    """Return the artifacts whose lineage never reaches primary data.

    An artifact reaches a primary one by ``derived_from``-type edges to its
    parents, or through the operation that produced it and the artifacts that
    operation took as input. A result that cannot be walked back to the photons
    (or the image, or the vendor file) is one nobody can check.

    Parameters
    ----------
    graph : dict
        A provenance graph.
    primary_kinds : tuple of str, optional
        Artifact kinds that count as primary data.

    Returns
    -------
    list of str
        Ids of the artifacts that do not reach primary data, sorted. Primary
        artifacts themselves are never listed.
    """
    parents: dict[str, set[str]] = {}
    producer: dict[str, set[str]] = {}
    inputs: dict[str, set[str]] = {}
    for e in graph["edges"]:
        kind = e["relationship_type"]
        if kind == "produced":
            producer.setdefault(e["target_node_id"], set()).add(e["source_node_id"])
        elif kind == "input_to":
            inputs.setdefault(e["target_node_id"], set()).add(e["source_node_id"])
        elif e["source_node_type"] == "artifact" and e["target_node_type"] == "artifact":
            parents.setdefault(e["source_node_id"], set()).add(e["target_node_id"])
    primary = {a for a, row in graph["artifacts"].items() if row["artifact_kind"] in primary_kinds}

    def reaches(start: str) -> bool:
        seen, queue = {start}, [start]
        while queue:
            current = queue.pop()
            if current in primary:
                return True
            nxt = set(parents.get(current, ()))
            for op in producer.get(current, ()):
                nxt |= inputs.get(op, set())
            for item in nxt - seen:
                seen.add(item)
                queue.append(item)
        return False

    return sorted(a for a in graph["artifacts"] if a not in primary and not reaches(a))


# -- from a .pto ---------------------------------------------------------------


def graph_from_pto(path: str | Path) -> Graph:
    """Read the provenance graph of a ``.pto`` container.

    Parameters
    ----------
    path : str or Path
        A measurement container (PTO.MFDB) or a ``*.cs.pto`` project.

    Returns
    -------
    dict
        The provenance graph.
    """
    import tttrlib

    handle = tttrlib.PtoFile()
    if not handle.open(str(path)):
        raise ValueError(handle.error())
    try:
        profile = {t.name: t.text for t in handle.tags_for(0)}.get("chisurf.profile", "")
        if profile == "ChiSurf.Project":
            return _graph_from_project(Path(path))
        return _graph_from_measurement(handle)
    finally:
        handle.close()


def _graph_from_measurement(handle: Any) -> Graph:
    """Build the graph from the per-object tags of a PTO.MFDB measurement."""
    import tttrlib

    def tag(uid: int, item: str) -> Any:
        return _clean(tttrlib.pto_tag(handle, uid, item, ""))

    objects = list(handle.objects())
    artifact_ids: dict[int, str] = {}
    artifacts: dict[str, dict] = {}
    for obj in objects:
        art_id = tag(obj.uid, "_mmfdb_artifact.artifact_id")
        if art_id is None:
            continue
        artifact_ids[obj.uid] = str(art_id)
        artifacts[str(art_id)] = {
            "artifact_kind": obj.kind,
            "data_format": tag(obj.uid, "_mmfdb_artifact.data_format"),
            "row_grain": tag(obj.uid, "_mmfdb_artifact.row_grain"),
            "checksum": tag(obj.uid, "_mmfdb_artifact.checksum"),
            "checksum_algorithm": tag(obj.uid, "_mmfdb_artifact.checksum_algorithm"),
        }

    # One operation per distinct run: the outputs of a single run (an MLE fit
    # writes one table per detector) share type, settings and parents.
    operations: dict[str, dict] = {}
    runs: dict[tuple, str] = {}
    edges: list[dict] = []
    for obj in objects:
        art_id = artifact_ids.get(obj.uid)
        op_type = tag(obj.uid, "_mmfdb_operation.operation_type")
        if art_id is None or op_type is None:
            continue
        parents = [artifact_ids[p] for p in tttrlib.pto_parents(handle, obj.uid) if p in artifact_ids]
        settings_text = tag(obj.uid, "_mmfdb_operation.settings_json")
        settings = json.loads(settings_text) if settings_text else None
        software_package = tag(obj.uid, "_mmfdb_operation.software_package")
        software_version = tag(obj.uid, "_mmfdb_operation.software_version")
        algorithm = tag(obj.uid, "_mmfdb_operation.algorithm")
        settings_hash = tag(obj.uid, "_mmfdb_operation.settings_hash") or _hash(settings)
        run = (op_type, algorithm, settings_hash, software_package, software_version,
               tuple(sorted(parents)))
        op_id = runs.get(run)
        if op_id is None:
            op_id = runs[run] = f"op:{art_id}"
            operations[op_id] = {
                "operation_type": op_type, "algorithm": algorithm,
                "software_package": software_package, "software_version": software_version,
                "status": "succeeded", "settings": settings, "settings_hash": settings_hash,
            }
            for parent in parents:
                edges.append(_edge("artifact", parent, "operation", op_id, "input_to",
                                   operation_id=op_id, role="input"))
        edges.append(_edge("operation", op_id, "artifact", art_id, "produced",
                           operation_id=op_id, role="output"))
        relationship = tag(obj.uid, "_mmfdb_edge.relationship_type") or "derived_from"
        for parent in parents:
            edges.append(_edge("artifact", art_id, "artifact", parent, str(relationship),
                               operation_id=op_id,
                               source_row_column=tag(obj.uid, "_mmfdb_edge.source_row_column"),
                               target_row_column=tag(obj.uid, "_mmfdb_edge.target_row_column")))
    return canonical({"operations": operations, "artifacts": artifacts,
                      "edges": edges, "parameters": []})


def _graph_from_project(path: Path) -> Graph:
    """Decompose a ``*.cs.pto`` project the way the archiver does, then read the tables."""
    from mmfdb.project import pto as project_pto
    from mmfdb.project.project_archiver import archive_project_to_mmfdb
    from mmfdb.repository import MFDatabase

    entries = project_pto.read_bundle(path.read_bytes())
    payload = json.loads(entries.pop("project.json"))
    version_id = "ver_" + uuid.uuid5(uuid.NAMESPACE_URL, hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode()).hexdigest()).hex
    with tempfile.TemporaryDirectory(prefix="mmfdb-graph-") as scratch:
        with MFDatabase(str(Path(scratch) / "graph.sqlite")) as db:
            archive_project_to_mmfdb(
                db, payload, version_id, "proj_" + version_id[4:], 1,
                resource_bundle=dict(entries),
            )
            return graph_from_db(db)


def _edge(source_type: str, source_id: str, target_type: str, target_id: str,
          relationship: str, **extra: Any) -> dict:
    """Return one edge with every item present, missing ones as None."""
    row = {"source_node_type": source_type, "source_node_id": source_id,
           "target_node_type": target_type, "target_node_id": target_id,
           "relationship_type": relationship}
    for item in _EDGE_ITEMS:
        row.setdefault(item, _clean(extra.get(item)))
    return row


def _hash(settings: Any) -> str | None:
    """Hash of settings when the container recorded none."""
    if settings is None:
        return None
    return hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest()[:16]


# -- from and into the database --------------------------------------------------


def graph_from_db(db: Any) -> Graph:
    """Read every live operation, artifact, edge, link and parameter of *db*.

    Parameters
    ----------
    db : MFDatabase
        An open database.

    Returns
    -------
    dict
        The provenance graph.
    """
    def rows(sql: str) -> list[dict]:
        return [dict(r) for r in db.conn.execute(sql)]

    operations = {}
    for row in rows("SELECT * FROM mmfdb_operation WHERE deleted_at IS NULL"):
        settings = json.loads(row["settings_json"]) if row["settings_json"] else None
        operations[row["operation_id"]] = {
            **{item: _clean(row.get(item)) for item in _OPERATION_ITEMS},
            "settings": settings,
        }
    artifacts = {
        row["artifact_id"]: {
            **{item: _clean(row.get(item)) for item in _ARTIFACT_ITEMS},
            # The column defaults to sha256 whether or not a checksum was recorded.
            "checksum_algorithm": _clean(row.get("checksum_algorithm")) if row.get("checksum") else None,
        }
        for row in rows("SELECT * FROM mmfdb_artifact WHERE deleted_at IS NULL")
    }
    edges = [
        _edge(r["source_node_type"], r["source_node_id"], r["target_node_type"],
              r["target_node_id"], r["relationship_type"], operation_id=r["operation_id"],
              source_row_column=r["source_row_column"], target_row_column=r["target_row_column"])
        for r in rows("SELECT * FROM mmfdb_edge WHERE deleted_at IS NULL")
    ]
    for r in rows("SELECT * FROM mmfdb_operation_artifact WHERE deleted_at IS NULL"):
        if r["direction"] == "input":
            edges.append(_edge("artifact", r["artifact_id"], "operation", r["operation_id"],
                               "input_to", operation_id=r["operation_id"], role=r["role"]))
        else:
            edges.append(_edge("operation", r["operation_id"], "artifact", r["artifact_id"],
                               "produced", operation_id=r["operation_id"], role=r["role"]))
    parameters = [
        {"parameter_id": r["parameter_uuid"],
         **{item: _clean(r.get(item)) for item in _PARAMETER_ITEMS if item != "parameter_id"},
         "bounds_on": None if _clean(r.get("bounds_on")) is None else str(r["bounds_on"])}
        for r in rows("SELECT * FROM mmfdb_parameter WHERE deleted_at IS NULL")
    ]
    return canonical({"operations": operations, "artifacts": artifacts,
                      "edges": edges, "parameters": parameters})


def import_graph(db: Any, graph: Graph) -> None:
    """Record *graph* in *db* through the public recording calls.

    Parameters
    ----------
    db : MFDatabase
        An open database.
    graph : dict
        The graph to record.

    Raises
    ------
    ValueError
        If a term (operation type, artifact kind, relationship) is not in the
        controlled vocabulary; nothing is invented.
    """
    with db.transaction():
        for op_id, op in graph["operations"].items():
            db.record_operation(
                op_id, op["operation_type"], settings=op["settings"],
                software_package=op["software_package"], software_version=op["software_version"],
                status=op["status"] or "succeeded",
            )
            db.dao.update("mmfdb_operation", op_id, {
                "algorithm": op["algorithm"], "settings_hash": op["settings_hash"]})
        for art_id, art in graph["artifacts"].items():
            db.register_artifact(
                art_id, artifact_kind=art["artifact_kind"], data_format=art["data_format"],
                checksum=art["checksum"],
                checksum_algorithm=art["checksum_algorithm"] or "sha256",
            )
            db.dao.update("mmfdb_artifact", art_id, {"row_grain": art["row_grain"]})
        for p in graph["parameters"]:
            db.record_parameter(
                p["parameter_id"], p["operation_id"], p["name"], value=p["value"],
                standard_error=p["standard_error"], initial_value=p["initial_value"],
                lower_bound=p["lower_bound"], upper_bound=p["upper_bound"],
                bounds_on=p["bounds_on"], units=p["units"],
                parameter_type=p["parameter_type"] or "free", role=p["role"],
            )
        for ordinal, e in enumerate(graph["edges"]):
            if e["relationship_type"] in _LINK_EDGES:
                artifact_id = e["source_node_id"] if e["relationship_type"] == "input_to" \
                    else e["target_node_id"]
                db.record_operation_link(
                    e["operation_id"], artifact_id, _LINK_EDGES[e["relationship_type"]],
                    role=e["role"], ordinal=ordinal,
                )
                continue
            db.add_edge(
                e["source_node_type"], e["source_node_id"], e["target_node_type"],
                e["target_node_id"], e["relationship_type"], operation_id=e["operation_id"],
            )
            if e["source_row_column"] or e["target_row_column"]:
                edge_id = db.conn.execute("SELECT max(edge_id) FROM mmfdb_edge").fetchone()[0]
                db.dao.update("mmfdb_edge", edge_id, {
                    "source_row_column": e["source_row_column"],
                    "target_row_column": e["target_row_column"]})


# -- the mmCIF form -------------------------------------------------------------


def write_graph_cif(graph: Graph, path: str | Path | None = None) -> str:
    """Write *graph* as ``_mmfdb_provenance_*`` mmCIF.

    Parameters
    ----------
    graph : dict
        The provenance graph.
    path : str or Path, optional
        Also write the text here.

    Returns
    -------
    str
        The mmCIF text.
    """
    graph = canonical(graph)
    buf = io.StringIO()
    writer = CifWriter(buf)
    writer.start_block("mmfdb_provenance")
    with writer.loop("_audit_conform", ["dict_name", "dict_version"]) as loop:
        loop.write(dict_name=WORKFLOW_DICTIONARY, dict_version="1.0")
    with writer.loop("_mmfdb_provenance_operation",
                     ["ordinal", "operation_id", *_OPERATION_ITEMS[:5], "settings_hash",
                      "settings_json"]) as loop:
        for i, (op_id, op) in enumerate(graph["operations"].items(), start=1):
            loop.write(ordinal=i, operation_id=op_id, settings_json=_dumps(op["settings"]),
                       **{k: op[k] for k in (*_OPERATION_ITEMS[:5], "settings_hash")})
    with writer.loop("_mmfdb_provenance_artifact",
                     ["ordinal", "artifact_id", *_ARTIFACT_ITEMS]) as loop:
        for i, (art_id, art) in enumerate(graph["artifacts"].items(), start=1):
            loop.write(ordinal=i, artifact_id=art_id, **art)
    with writer.loop("_mmfdb_provenance_edge", ["ordinal", *_EDGE_ITEMS]) as loop:
        for i, edge in enumerate(graph["edges"], start=1):
            loop.write(ordinal=i, **edge)
    with writer.loop("_mmfdb_provenance_parameter", ["ordinal", *_PARAMETER_ITEMS]) as loop:
        for i, p in enumerate(graph["parameters"], start=1):
            loop.write(ordinal=i, **{k: (repr(v) if k in _FLOAT_PARAMETER_ITEMS and v is not None
                                         else v) for k, v in p.items()})
    text = buf.getvalue()
    if path is not None:
        Path(path).write_text(text, encoding="utf-8")
    return text


def _dumps(value: Any) -> str | None:
    """Compact, key-sorted JSON; None stays None."""
    return None if value is None else json.dumps(value, sort_keys=True, separators=(",", ":"))


def category_handlers(sink: dict[str, list[dict]], categories: list[str] | None = None) -> list:
    """Build python-ihm handlers that collect the rows of ``_mmfdb_*`` categories.

    python-ihm warns (``UnknownCategoryWarning``) about a category no handler
    claims. Declaring the dictionary's categories to the reader is what makes a
    file full of them readable without the warning, and is how their rows are
    recovered.

    Parameters
    ----------
    sink : dict
        Receives ``{category: [row, ...]}`` while the file is read.
    categories : list of str, optional
        Category names including the leading underscore. Default: every
        ``mmfdb_`` category of the bundled and export-only dictionaries, and
        ``_audit_conform``.

    Returns
    -------
    list
        ``ihm.reader.Handler`` subclasses, for ``ihm.reader.read(handlers=...)``.
    """
    import ihm.reader

    from mmfdb.schema.pdbx_metadata import MmcifDictionary

    dictionary = MmcifDictionary(*[
        MmcifDictionary.DATA_DIR / n
        for n in MmcifDictionary.BUNDLED_DICTS + MmcifDictionary.EXPORT_ONLY_DICTS])
    names = categories or [
        # python-ihm has no handler for the dictionary-conformance loop either.
        "_audit_conform",
        *(f"_{c}" for c in dictionary.categories() if c.startswith("mmfdb_")),
    ]
    handlers = []
    for category in names:
        category_def = dictionary.get_category(category.lstrip("_"))
        items = [i.rsplit(".", 1)[-1] for i in (category_def.items if category_def else {})]
        if not items:
            continue
        sink.setdefault(category, [])
        handlers.append(_make_handler(category, items, sink[category]))
    return handlers


def _make_handler(category: str, items: list[str], rows: list[dict]) -> type:
    """One handler class whose ``__call__`` takes exactly the category's items."""
    import ihm.reader

    parameters = [inspect.Parameter("self", inspect.Parameter.POSITIONAL_OR_KEYWORD)]
    parameters += [inspect.Parameter(i, inspect.Parameter.POSITIONAL_OR_KEYWORD) for i in items]

    def __call__(self, *values: Any) -> None:
        rows.append(dict(zip(items, values)))

    __call__.__signature__ = inspect.Signature(parameters)  # type: ignore[attr-defined]
    return type(f"Handler{category}", (ihm.reader.Handler,),
                {"category": category, "__call__": __call__})


def read_graph_cif(path: str | Path, tolerate: Iterable[str] = ()) -> Graph:
    """Read a graph CIF with python-ihm and return the graph.

    Parameters
    ----------
    path : str or Path
        A file written by :func:`write_graph_cif` or ``write_single_cif``.
    tolerate : iterable of str, optional
        Category names (with the leading underscore) that another reader owns
        and that therefore need not be reported as unknown here.

    Returns
    -------
    dict
        The provenance graph. An unknown category, or an unknown keyword of an
        ``_mmfdb_`` category, raises a warning, which the test suite turns into
        an error.
    """
    import ihm.reader

    sink: dict[str, list[dict]] = {}
    with warnings.catch_warnings(record=True) as caught, open(path, encoding="utf-8") as handle:
        warnings.simplefilter("always")
        ihm.reader.read(handle, handlers=category_handlers(sink),
                        warn_unknown_category=True, warn_unknown_keyword=True)
    for w in caught:
        # An unknown keyword in a category this module does not own (the sample
        # and analysis export writes database column names under flrCIF
        # categories) is not a provenance defect; anything else is passed on.
        if issubclass(w.category, ihm.reader.UnknownKeywordWarning) and "_mmfdb_" not in str(w.message):
            continue
        if issubclass(w.category, ihm.reader.UnknownCategoryWarning) and any(
                f"category {name} " in str(w.message) for name in tolerate):
            continue
        warnings.warn(w.message, w.category, stacklevel=2)

    def text(v: Any) -> Any:
        return None if v is None or v is ihm.unknown else v

    def number(v: Any) -> Any:
        return None if text(v) is None else float(v)

    operations = {}
    for r in sink.get("_mmfdb_provenance_operation", []):
        operations[r["operation_id"]] = {
            **{k: text(r.get(k)) for k in (*_OPERATION_ITEMS[:5], "settings_hash")},
            "settings": json.loads(r["settings_json"]) if text(r.get("settings_json")) else None,
        }
    artifacts = {
        r["artifact_id"]: {k: text(r.get(k)) for k in _ARTIFACT_ITEMS}
        for r in sink.get("_mmfdb_provenance_artifact", [])
    }
    edges = [{k: text(r.get(k)) for k in _EDGE_ITEMS}
             for r in sink.get("_mmfdb_provenance_edge", [])]
    parameters = []
    for r in sink.get("_mmfdb_provenance_parameter", []):
        row = {k: text(r.get(k)) for k in _PARAMETER_ITEMS}
        for k in _FLOAT_PARAMETER_ITEMS:
            row[k] = number(r.get(k))
        parameters.append(row)
    return canonical({"operations": operations, "artifacts": artifacts,
                      "edges": edges, "parameters": parameters})
