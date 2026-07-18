"""Export a workflow run for publication: methods report, mmCIF, and a bundle.

Three publication targets, all built from the provenance MMFDB already recorded:

* :func:`methods_report` — a Markdown methods section listing every tool,
  version, command line, and parameter set, in execution order.
* :func:`write_mmcif` — a standards mmCIF/FLR metadata file, reusing
  :meth:`mmfdb.repository.MFDatabase.export_flr_cif_to_text`.
* :func:`write_bundle` — a self-contained ZIP (database snapshot, provenance
  graph, the methods report, the mmCIF, and the native data files) that a
  second MMFDB instance can be reconstituted from and that uploads as-is to
  Zenodo or OSF.
"""

from __future__ import annotations

import base64
import io
import json
import shutil
import tempfile
import textwrap
import zipfile
from pathlib import Path
from typing import Any

from mmfdb.security.base import MMFDBClientBase


def methods_report(
    db: MMFDBClientBase, seed_node_id: str, *, seed_node_type: str = "artifact", title: str = ""
) -> str:
    """Render a Markdown methods section from the provenance around a seed node."""
    graph = db.export_provenance_graph(seed_node_type, seed_node_id)
    operations = [n for n in graph["nodes"] if _norm(n.get("node_type")) == "operation"]
    operations.sort(key=lambda n: (n.get("started_at") or "", n.get("created_at") or ""))
    artifacts = [n for n in graph["nodes"] if _norm(n.get("node_type")) == "artifact"]

    lines: list[str] = []
    lines.append(f"# {title or 'Data analysis workflow'}")
    lines.append("")
    lines.append(
        f"Provenance was recorded in MMFDB and exported from node `{seed_node_id}`. "
        f"The analysis comprised {len(operations)} operation(s)."
    )
    lines.append("")
    lines.append("## Methods")
    lines.append("")
    if not operations:
        lines.append("_No recorded operations were found for this node._")
    for index, op in enumerate(operations, start=1):
        lines.extend(_describe_operation(index, op))
        lines.append("")

    lines.append("## Data artifacts")
    lines.append("")
    lines.append("| artifact | kind | format | checksum |")
    lines.append("| --- | --- | --- | --- |")
    for art in sorted(artifacts, key=lambda n: str(n.get("node_id"))):
        lines.append(
            f"| `{art.get('node_id')}` | {art.get('artifact_kind') or ''} | "
            f"{art.get('data_format') or ''} | `{art.get('checksum') or ''}` |"
        )
    lines.append("")
    return "\n".join(lines)


def _describe_operation(index: int, op: dict[str, Any]) -> list[str]:
    settings = _load_json(op.get("settings_json"))
    package = op.get("software_package")
    version = op.get("software_version")
    op_type = op.get("operation_type") or "analysis"

    # Source registration is stamped with the "mmfdb" package (see the runner);
    # it is data intake, not an analysis tool, so describe it as such.
    if op_type == "measurement_import" or package == "mmfdb":
        return [f"### Step {index}: data registration", "", "Input data registered into MMFDB."]

    tool = f"{package} {version}" if version else (package or "an unspecified tool")
    out: list[str] = [f"### Step {index}: {op_type}", ""]
    out.append(f"Performed with **{tool}**.")
    command_line = settings.get("command_line")
    if command_line:
        out.append("")
        out.append("```")
        out.append(str(command_line))
        out.append("```")
    params = settings.get("params")
    if isinstance(params, dict) and params:
        rendered = ", ".join(f"`{k}` = {v}" for k, v in params.items())
        out.append(f"Parameters: {rendered}.")
    summary = settings.get("tool_summary")
    if isinstance(summary, dict) and summary:
        highlights = ", ".join(f"{k} = {v}" for k, v in summary.items() if not isinstance(v, (list, dict)))
        if highlights:
            out.append(f"Reported: {highlights}.")
    return out


def write_mmcif(db: MMFDBClientBase, path: str | Path) -> Path:
    """Write a standards mmCIF/FLR metadata file for the database."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(db.export_flr_cif_to_text(), encoding="utf-8")
    return target


_BUNDLE_README = """\
This is an MMFDB publication bundle. It is readable without MMFDB or any
database engine — everything is a standard, self-describing format:

  metadata.cif          instrument, sample, and analysis metadata (mmCIF / PDBx-FLR)
  provenance_graph.json  the source -> tool -> result graph (nodes + edges)
  methods.md            a human-readable methods section
  manifest.json         every data file with its original name and checksum
  external_data/        the data files themselves, kept in their original format

Each entry in external_data/ is listed in manifest.json with "kept_original":
true when it is the untouched original file. To verify integrity, re-hash a file
and compare it against its "checksum" in manifest.json.

{snapshot_note}"""

_SNAPSHOT_NOTE = (
    "database_snapshot.db is an optional SQLite copy of the full MMFDB record, "
    "included here for exact re-import into another MMFDB instance. It is not "
    "required to read anything above."
)


def write_bundle(
    db: MMFDBClientBase,
    seed_node_id: str,
    zip_path: str | Path,
    *,
    seed_node_type: str = "artifact",
    include_data: bool = True,
    include_snapshot: bool = False,
) -> Path:
    """Write a publication bundle ZIP seeded at ``seed_node_id``.

    The bundle is deliberately readable without MMFDB: standards mmCIF metadata,
    a JSON provenance graph, a Markdown methods report, a checksummed manifest,
    and the original data files kept in their native format under
    ``external_data/``. Pass ``include_snapshot=True`` to also embed a SQLite
    ``database_snapshot.db`` for exact re-import into another MMFDB instance —
    off by default so a deposition is not a proprietary binary blob.
    """
    target = Path(zip_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    graph = db.export_provenance_graph(seed_node_type, seed_node_id)

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        manifest: dict[str, Any] = {
            "archive_format_version": 2,
            "seed_node": {"type": seed_node_type, "id": seed_node_id},
            "nodes": len(graph["nodes"]),
            "edges": len(graph["edges"]),
            "includes_database_snapshot": include_snapshot,
            "files": [],
        }
        data_files: list[tuple[str, Path]] = []
        if include_data:
            data_files = _materialize_data(db, graph, tmpdir, manifest)

        readme = _BUNDLE_README.format(
            snapshot_note=_SNAPSHOT_NOTE if include_snapshot else ""
        ).rstrip() + "\n"

        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("README.txt", readme)
            zf.writestr("provenance_graph.json", json.dumps(graph, indent=2))
            zf.writestr("methods.md", methods_report(db, seed_node_id, seed_node_type=seed_node_type))
            zf.writestr("metadata.cif", db.export_flr_cif_to_text())
            zf.writestr("manifest.json", json.dumps(manifest, indent=2))
            for rel, src in data_files:
                zf.write(src, rel)
            if include_snapshot:
                snapshot = tmpdir / "database_snapshot.db"
                db.backup_database(str(snapshot))
                zf.write(snapshot, "database_snapshot.db")
    return target


WORKFLOW_DICTIONARY = "mmfdb_workflow_ext.dic"


def workflow_dictionary_text() -> str:
    """Return the shipped mmCIF dictionary for the workflow/provenance categories."""
    from importlib.resources import files

    return (files("mmfdb").joinpath("data", WORKFLOW_DICTIONARY)).read_text(encoding="utf-8")


def write_single_cif(
    db: MMFDBClientBase,
    seed_node_id: str,
    path: str | Path,
    *,
    seed_node_type: str = "artifact",
    workflow: Any = None,
    workflow_yaml: str | None = None,
    include_data: bool = True,
    embed_dictionary: bool = True,
) -> Path:
    """Write ONE self-contained, reproducible mmCIF deposit file.

    The single ``.cif`` carries everything needed to understand and re-run the
    analysis, with no external files:

    * the standard FLR/mmCIF metadata block,
    * the workflow's identity and steps (``mmfdb_workflow`` / ``_step``),
    * the recorded provenance graph (``mmfdb_provenance_operation`` /
      ``_artifact`` / ``_edge``) with each tool, version, command line, and
      parameter set,
    * every original data file **and** the workflow YAML, embedded base64
      (``mmfdb_bundle_file``) so the deposit is reproducible, and
    * the methods report, a JSON manifest, and this file's own dictionary
      (``mmfdb_bundle_document`` + ``mmfdb_workflow_ext.dic``), so the CIF is
      self-describing.

    The categories are defined in :data:`WORKFLOW_DICTIONARY`, referenced from an
    ``_audit_conform`` loop. Verbatim content that may contain CIF control
    characters (the YAML, the dictionary) is base64-embedded to stay valid.
    """
    from mmfdb.cif_writer import CifWriter

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    graph = db.export_provenance_graph(seed_node_type, seed_node_id)
    operations = sorted(
        (n for n in graph["nodes"] if _norm(n.get("node_type")) == "operation"),
        key=lambda n: (n.get("started_at") or "", n.get("created_at") or ""),
    )
    artifacts = sorted(
        (n for n in graph["nodes"] if _norm(n.get("node_type")) == "artifact"),
        key=lambda n: str(n.get("node_id")),
    )

    buf = io.StringIO()
    buf.write(db.export_flr_cif_to_text().rstrip() + "\n\n")
    writer = CifWriter(buf)
    writer.start_block("mmfdb_deposit")

    with writer.loop("_audit_conform", ["dict_name", "dict_version"]) as loop:
        loop.write(dict_name="mmfdb_flr_ext.dic", dict_version="1.0")
        loop.write(dict_name=WORKFLOW_DICTIONARY, dict_version="1.0")

    if workflow is not None:
        with writer.loop(
            "_mmfdb_workflow", ["name", "version", "description"]
        ) as loop:
            loop.write(
                name=getattr(workflow, "name", "workflow"),
                version=getattr(workflow, "version", None),
                description=getattr(workflow, "description", None) or None,
            )
        with writer.loop(
            "_mmfdb_workflow_step",
            ["ordinal", "step_id", "operation_type", "software_package", "software_version", "run_kind", "run"],
        ) as loop:
            for i, step in enumerate(workflow.ordered_steps(), start=1):
                loop.write(
                    ordinal=i,
                    step_id=step.id,
                    operation_type=step.operation_type,
                    software_package=step.software.package or None,
                    software_version=step.software.version or None,
                    run_kind=step.kind,
                    run=step.cmd or step.python,
                )

    with writer.loop(
        "_mmfdb_provenance_operation",
        ["ordinal", "operation_id", "operation_type", "software_package", "software_version",
         "status", "command_line", "parameters", "started_at", "ended_at"],
    ) as loop:
        for i, op in enumerate(operations, start=1):
            settings = _load_json(op.get("settings_json"))
            params = settings.get("params")
            loop.write(
                ordinal=i,
                operation_id=op.get("node_id"),
                operation_type=op.get("operation_type"),
                software_package=op.get("software_package"),
                software_version=op.get("software_version"),
                status=op.get("status"),
                command_line=settings.get("command_line"),
                parameters=json.dumps(params) if params else None,
                started_at=op.get("started_at"),
                ended_at=op.get("ended_at"),
            )

    with writer.loop(
        "_mmfdb_provenance_artifact",
        ["ordinal", "artifact_id", "artifact_kind", "data_format", "checksum", "checksum_algorithm"],
    ) as loop:
        for i, art in enumerate(artifacts, start=1):
            loop.write(
                ordinal=i,
                artifact_id=art.get("node_id"),
                artifact_kind=art.get("artifact_kind"),
                data_format=art.get("data_format"),
                checksum=art.get("checksum"),
                checksum_algorithm=art.get("checksum_algorithm"),
            )

    with writer.loop(
        "_mmfdb_provenance_edge",
        ["ordinal", "source_node_type", "source_node_id", "target_node_type", "target_node_id", "relationship_type"],
    ) as loop:
        for i, edge in enumerate(graph["edges"], start=1):
            loop.write(
                ordinal=i,
                source_node_type=edge.get("source_node_type"),
                source_node_id=edge.get("source_node_id"),
                target_node_type=edge.get("target_node_type"),
                target_node_id=edge.get("target_node_id"),
                relationship_type=edge.get("relationship_type"),
            )

    manifest: dict[str, Any] = {
        "archive_format_version": 3,
        "container": "single_cif",
        "seed_node": {"type": seed_node_type, "id": seed_node_id},
        "nodes": len(graph["nodes"]),
        "edges": len(graph["edges"]),
        "dictionary": WORKFLOW_DICTIONARY,
        "files": [],
    }
    extras: list[tuple[str, str, bytes]] = []
    if workflow_yaml:
        extras.append(("workflow.yaml", "application/x-yaml", workflow_yaml.encode("utf-8")))
    if embed_dictionary:
        extras.append((WORKFLOW_DICTIONARY, "application/x-pdbx-dictionary", workflow_dictionary_text().encode("utf-8")))

    if include_data or extras:
        used: set[str] = set()
        ordinal = 0
        with writer.loop(
            "_mmfdb_bundle_file",
            ["ordinal", "artifact_id", "name", "data_format", "media_type", "size_bytes",
             "sha256", "md5", "encoding", "content"],
        ) as loop:
            if include_data:
                for node in artifacts:
                    object_uuid = node.get("object_uuid")
                    if not object_uuid:
                        continue
                    try:
                        info = db.get_object_info(object_uuid) or {}
                        data = db.get_object(object_uuid)
                    except Exception:  # pragma: no cover - best-effort inclusion
                        continue
                    ordinal += 1
                    name = _data_file_name(info, node, node.get("node_id"), used)
                    used.add(name)
                    loop.write(
                        ordinal=ordinal,
                        artifact_id=node.get("node_id"),
                        name=name,
                        data_format=node.get("data_format"),
                        media_type=_media_type(node.get("data_format")),
                        size_bytes=len(data),
                        sha256=info.get("content_sha256"),
                        md5=info.get("content_md5"),
                        encoding="base64",
                        content=_b64(data),
                    )
                    manifest["files"].append(
                        {"name": name, "artifact_id": node.get("node_id"),
                         "kept_original": bool(info.get("original_filename")),
                         "sha256": info.get("content_sha256"), "encoding": "base64"}
                    )
            for name, media, payload in extras:
                if name in used:
                    continue
                ordinal += 1
                used.add(name)
                loop.write(
                    ordinal=ordinal, artifact_id=None, name=name,
                    data_format=name.rsplit(".", 1)[-1], media_type=media,
                    size_bytes=len(payload), sha256=_sha256_bytes(payload), md5=None,
                    encoding="base64", content=_b64(payload),
                )
                manifest["files"].append(
                    {"name": name, "artifact_id": None, "kept_original": False,
                     "sha256": _sha256_bytes(payload), "encoding": "base64"}
                )

    with writer.loop("_mmfdb_bundle_document", ["name", "media_type", "content"]) as loop:
        loop.write(
            name="methods.md", media_type="text/markdown",
            content=methods_report(db, seed_node_id, seed_node_type=seed_node_type),
        )
        loop.write(name="manifest.json", media_type="application/json", content=json.dumps(manifest, indent=2))

    target.write_text(buf.getvalue(), encoding="utf-8")
    return target


def read_single_cif(path: str | Path) -> dict[str, Any]:
    """Extract the embedded files and documents from a single deposit CIF.

    Returns ``{"files": {name: bytes}, "documents": {name: text}}`` — the inverse
    of :func:`write_single_cif`, so a deposit can be unpacked and re-run without
    MMFDB. Round-tripping the data is what makes the deposit reproducible.
    """
    text = Path(path).read_text(encoding="utf-8")
    loops = {category: (columns, rows) for category, columns, rows in _parse_cif_loops(text)}
    files: dict[str, bytes] = {}
    documents: dict[str, str] = {}
    if "_mmfdb_bundle_file" in loops:
        _columns, rows = loops["_mmfdb_bundle_file"]
        for row in rows:
            name = row.get("name")
            if name and name != ".":
                files[name] = base64.b64decode(row.get("content", ""))
    if "_mmfdb_bundle_document" in loops:
        _columns, rows = loops["_mmfdb_bundle_document"]
        for row in rows:
            name = row.get("name")
            if name and name != ".":
                documents[name] = row.get("content", "")
    return {"files": files, "documents": documents}


def _b64(data: bytes) -> str:
    return "\n".join(textwrap.wrap(base64.b64encode(data).decode("ascii"), 76))


def _sha256_bytes(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def _media_type(data_format: Any) -> str:
    return {
        "csv": "text/csv",
        "json": "application/json",
        "hdf5": "application/x-hdf5",
        "h5": "application/x-hdf5",
        "txt": "text/plain",
    }.get(str(data_format or "").lstrip("."), "application/octet-stream")


def export_publication(
    db: MMFDBClientBase,
    seed_node_id: str,
    out_dir: str | Path,
    formats: tuple[str, ...] | list[str],
    *,
    name: str = "workflow",
    seed_node_type: str = "artifact",
    workflow: Any = None,
    workflow_yaml: str | None = None,
) -> dict[str, str]:
    """Write the requested publication artifacts and return a name -> path map."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    produced: dict[str, str] = {}
    for fmt in formats:
        if fmt == "report":
            path = out / f"{name}_methods.md"
            path.write_text(
                methods_report(db, seed_node_id, seed_node_type=seed_node_type, title=name),
                encoding="utf-8",
            )
            produced["report"] = str(path)
        elif fmt == "mmcif":
            produced["mmcif"] = str(write_mmcif(db, out / f"{name}.cif"))
        elif fmt == "bundle":
            produced["bundle"] = str(
                write_bundle(db, seed_node_id, out / f"{name}.zip", seed_node_type=seed_node_type)
            )
        elif fmt == "archive":
            produced["archive"] = str(
                write_bundle(
                    db,
                    seed_node_id,
                    out / f"{name}_archive.zip",
                    seed_node_type=seed_node_type,
                    include_snapshot=True,
                )
            )
        elif fmt == "cif":
            produced["cif"] = str(
                write_single_cif(
                    db,
                    seed_node_id,
                    out / f"{name}.deposit.cif",
                    seed_node_type=seed_node_type,
                    workflow=workflow,
                    workflow_yaml=workflow_yaml,
                )
            )
    return produced


def _parse_cif_loops(text: str):
    """Yield ``(category, columns, rows)`` for every ``loop_`` in a CIF document.

    A pragmatic reader (not a full CIF parser) sufficient to recover MMFDB's own
    deposit loops: it understands ``;``-delimited text blocks and simple quoting,
    and groups the flat token stream into rows by column count.
    """
    lines = text.splitlines()
    i, n = 0, len(lines)
    while i < n:
        if lines[i].strip() != "loop_":
            i += 1
            continue
        i += 1
        columns: list[str] = []
        while i < n and lines[i].strip().startswith("_"):
            columns.append(lines[i].strip())
            i += 1
        tokens: list[str] = []
        while i < n:
            raw = lines[i]
            stripped = raw.strip()
            if stripped == "" or stripped == "#":
                i += 1
                if stripped == "#":
                    break
                continue
            if stripped == "loop_" or stripped.startswith(("data_", "save_")) or stripped == "stop_":
                break
            if raw.startswith(";"):
                block = [raw[1:]]
                i += 1
                while i < n and not lines[i].startswith(";"):
                    block.append(lines[i])
                    i += 1
                i += 1  # closing ';'
                tokens.append("\n".join(block).strip("\n"))
                continue
            if stripped.startswith("_"):
                break
            tokens.extend(_split_cif_line(stripped))
            i += 1
        category = columns[0].split(".", 1)[0] if columns else ""
        names = [c.split(".", 1)[1] if "." in c else c for c in columns]
        width = len(names)
        rows = [
            dict(zip(names, tokens[k:k + width]))
            for k in range(0, len(tokens) - width + 1, width)
        ] if width else []
        yield category, names, rows


def _split_cif_line(line: str) -> list[str]:
    tokens: list[str] = []
    i, n = 0, len(line)
    while i < n:
        while i < n and line[i] == " ":
            i += 1
        if i >= n:
            break
        if line[i] in "'\"":
            quote = line[i]
            i += 1
            start = i
            while i < n and line[i] != quote:
                i += 1
            tokens.append(line[start:i])
            i += 1
        else:
            start = i
            while i < n and line[i] != " ":
                i += 1
            tokens.append(line[start:i])
    return tokens


def _materialize_data(
    db: MMFDBClientBase, graph: dict[str, Any], tmpdir: Path, manifest: dict[str, Any]
) -> list[tuple[str, Path]]:
    data_dir = tmpdir / "external_data"
    data_dir.mkdir(exist_ok=True)
    files: list[tuple[str, Path]] = []
    used: set[str] = set()
    for node in graph["nodes"]:
        if _norm(node.get("node_type")) != "artifact":
            continue
        artifact_id = node.get("node_id")
        object_uuid = node.get("object_uuid")
        if not artifact_id or not object_uuid:
            continue
        try:
            info = db.get_object_info(object_uuid) or {}
            blob_path = db.get_object_path(object_uuid)
        except Exception:  # pragma: no cover - best-effort data inclusion
            continue
        name = _data_file_name(info, node, artifact_id, used)
        used.add(name)
        dest = data_dir / name
        shutil.copy2(blob_path, dest)
        rel = f"external_data/{name}"
        files.append((rel, dest))
        manifest["files"].append(
            {
                "node_id": artifact_id,
                "relative_path": rel,
                "original_filename": info.get("original_filename"),
                "kept_original": bool(info.get("original_filename")),
                "data_format": node.get("data_format"),
                "checksum": node.get("checksum"),
            }
        )
    return files


def _data_file_name(
    info: dict[str, Any], node: dict[str, Any], artifact_id: str, used: set[str]
) -> str:
    """Pick a recognizable, unique filename for a materialized artifact."""
    name = info.get("original_filename")
    if not name:
        suffix = str(node.get("data_format") or "").lstrip(".")
        name = f"{artifact_id}.{suffix}" if suffix else artifact_id
    if name not in used:
        return name
    stem, dot, suffix = name.partition(".")
    prefix = artifact_id[:8]
    candidate = f"{stem}_{prefix}{dot}{suffix}"
    while candidate in used:  # pragma: no cover - extremely unlikely
        prefix += "x"
        candidate = f"{stem}_{prefix}{dot}{suffix}"
    return candidate


def _load_json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _norm(node_type: Any) -> str:
    from mmfdb.provenance.graph import normalize_node_type

    return normalize_node_type(str(node_type or ""))
