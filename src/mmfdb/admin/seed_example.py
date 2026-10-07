"""Seed a synthetic burst-table metadata and provenance demonstration.

The small CSV tables come from MMFDB's workflow examples. This is an
administrative demo, not an SPC acquisition or photon-analysis pipeline.
"""

from __future__ import annotations

import csv
import hashlib
import io
import logging
import math
import uuid as _uuid
from datetime import datetime, timezone
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path

from mmfdb.repository import MFDatabase
from mmfdb.samples.sample_manager import link_artifact_to_sample
from mmfdb.store.database_resolver import resolve_database_path

logger = logging.getLogger(__name__)

DEMO_FILES = ("bursts.csv", "bursts_rep1.csv", "bursts_rep2.csv")
DEMO_COLUMNS = ("burst_id", "size", "proximity_ratio")
SELECTION_MIN = 0.5

DEMO = dict(
    user_id="john_doe",
    device_id="mt200",
    entity_id="sm_dna_demo",
    assembly_id="sm_dna_a488_a647",
    condition_id="pbs_ph74_25c",
    sample_id="sm_dna_a488_a647_sample",
    experiment_id="sm_dna_mt200_001",
    project_id="demo_sm_fret",
    processing_id="burst_selection_sm_dna_001",
)

DNA_SEQ = list("GAAGTCGAGATGGCTCGAGA")


def _upsert_probe(
    db: MFDatabase, name: str, type_id: int, *, category: str,
    abs_max: float, em_max: float, qy: float, ext_coeff: float | None = None,
) -> int:
    rows = db.dao.list(
        "probes", filters={"chromophore_name": name, "type_id": type_id}, limit=1
    )
    if rows:
        return int(rows[0]["probe_id"])
    probe_id = db.add_probe(name, type_id, category=category)
    db.add_optical_property(probe_id, "abs_max", abs_max, unit="nm")
    db.add_optical_property(probe_id, "em_max", em_max, unit="nm")
    db.add_optical_property(probe_id, "qy", qy, unit="")
    if ext_coeff is not None:
        db.add_optical_property(probe_id, "ext_coeff", ext_coeff, unit="M-1 cm-1")
    logger.info("Created probe %s (id=%d)", name, probe_id)
    return probe_id


def _upsert_user(db: MFDatabase, user_id: str, display_name: str, email: str | None = None) -> None:
    if db.dao.get("flr_sample_users", user_id) is not None:
        return
    db.add_user(user_id, display_name, email=email)
    logger.info("Created user %s (%s)", user_id, display_name)


def _upsert_device(
    db: MFDatabase, device_id: str, name: str, /,
    device_type: str | None = None, model: str | None = None, serial: str | None = None,
) -> None:
    if db.dao.get("flr_sample_devices", device_id) is not None:
        return
    db.add_device(device_id, name, device_type=device_type, model=model, serial_number=serial)
    logger.info("Created device %s (%s)", device_id, name)


def _upsert_entity(db: MFDatabase, entity_id: str, seq: list[str]) -> None:
    """Seed an entity + poly-seq via the dao; add_entity API is schema-incompatible."""
    if db.dao.get("entities", entity_id) is not None:
        return
    with db.conn:
        db.dao.insert("entities", {
            "entity_id": entity_id,
            "type": "polymer",
            "description": "Demo DNA smFRET sample labeled with Alexa488/Alexa647",
            "common_name": "Demo DNA",
        })
        for i, mon_id in enumerate(seq, start=1):
            db.dao.insert("entity_poly_seq", {"entity_id": entity_id, "num": i, "mon_id": mon_id})
    logger.info("Created entity %s (%d bp)", entity_id, len(seq))


def _upsert_entity_assembly(db: MFDatabase, assembly_id: str, description: str) -> None:
    if db.dao.get("flr_entity_assembly", assembly_id) is not None:
        return
    with db.conn:
        db.dao.insert("flr_entity_assembly", {"assembly_id": assembly_id, "description": description})


def _upsert_sample_condition(
    db: MFDatabase, condition_id: str, *,
    ph: float, temperature: float, buffer: str,
) -> None:
    if db.dao.get("flr_sample_condition", condition_id) is not None:
        return
    with db.conn:
        db.dao.insert("flr_sample_condition", {
            "condition_id": condition_id,
            "ph": ph,
            "temperature": temperature,
            "buffer_composition": buffer,
            "details": f"pH {ph} at {temperature} K",
        })


def _upsert_poly_probe_position(
    db: MFDatabase, probe_id: int, entity_id: str, residue: int, chain: str = "A",
) -> int:
    rows = db.dao.list("flr_poly_probe_position", filters={
        "probe_id": probe_id, "entity_id": entity_id,
        "asym_id": chain, "residue_number": residue,
    }, limit=1)
    if rows:
        return int(rows[0]["id"])
    with db.conn:
        return db.dao.insert("flr_poly_probe_position", {
            "probe_id": probe_id, "entity_id": entity_id, "asym_id": chain,
            "residue_number": residue, "residue_name": "DA",
        })


def _upsert_experiment_type(db: MFDatabase, name: str, category: str, description: str) -> int:
    rows = db.dao.list("flr_experiment_type", filters={"name": name}, limit=1)
    if rows:
        return int(rows[0]["type_id"])
    return db.add_experiment_type(name, category=category, description=description)


def _ensure_probe_types(db: MFDatabase) -> dict[str, int]:
    existing = {r["type_name"]: int(r["type_id"]) for r in db.get_probe_types()}
    for name, desc in [("organic_dye", "Organic dye"),
                        ("amino_acid", "Amino acid fluorophore"),
                        ("nucleic_acid", "Nucleic acid fluorophore")]:
        if name not in existing:
            with db.conn:
                db.dao.upsert(
                    "probe_types", {"type_name": name, "display_name": desc},
                    conflict=["type_name"],
                )
            rows = db.dao.list("probe_types", filters={"type_name": name}, limit=1)
            existing[name] = int(rows[0]["type_id"]) if rows else len(existing) + 1
    return existing


def _seed_flr_tables(db: MFDatabase) -> tuple[int, int, int, int]:
    """Seed the legacy flr_* tables with demo entities, returning key ids."""
    type_ids = _ensure_probe_types(db)
    dye_type = type_ids.get("organic_dye", 1)

    probe_a488 = _upsert_probe(
        db, "Alexa488", dye_type, category="organic_dye",
        abs_max=495.0, em_max=519.0, qy=0.92, ext_coeff=73000.0,
    )
    probe_a647 = _upsert_probe(
        db, "Alexa647", dye_type, category="organic_dye",
        abs_max=650.0, em_max=665.0, qy=0.33, ext_coeff=250000.0,
    )

    d = DEMO
    _upsert_user(db, d["user_id"], "John Doe", "john.doe@example.org")
    _upsert_device(db, d["device_id"], "MT200", device_type="TCSPC", model="MT200", serial="MT200-001")
    _upsert_entity(db, d["entity_id"], DNA_SEQ)
    _upsert_entity_assembly(db, d["assembly_id"],
                            "Demo DNA assembly labeled with Alexa488/Alexa647")
    _upsert_sample_condition(db, d["condition_id"], ph=7.4, temperature=298.15, buffer="PBS")

    pos_a488 = _upsert_poly_probe_position(db, probe_a488, d["entity_id"], 10)
    pos_a647 = _upsert_poly_probe_position(db, probe_a647, d["entity_id"], 20)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

    if not db.get_sample(d["sample_id"]):
        db.add_sample(
            d["sample_id"],
            uuid=str(_uuid.uuid4()),
            description="Demo DNA labeled with Alexa488/Alexa647 for smFRET",
            details="Synthetic burst-table demo context; not an acquired measurement",
            num_of_probes=2,
            solvent_phase="liquid",
            sample_condition_id=d["condition_id"],
            entity_assembly_id=d["assembly_id"],
            project_id=d["project_id"],
            measured_by_user_id=d["user_id"],
            measured_by_device_id=d["device_id"],
            measured_at=now,
        )
        logger.info("Created sample %s", d["sample_id"])

    if not db.get_sample_probe_mappings(d["sample_id"]):
        db.add_sample_probe(d["sample_id"], probe_a488, "donor",
                            description="Donor (Alexa488) at position 10",
                            poly_probe_position_id=pos_a488)
        db.add_sample_probe(d["sample_id"], probe_a647, "acceptor",
                            description="Acceptor (Alexa647) at position 20",
                            poly_probe_position_id=pos_a647)
        logger.info("Created sample-probe mappings")

    exp_type_id = _upsert_experiment_type(
        db, "single_molecule_alex", "Single-molecule",
        "Alternating-laser excitation single-molecule experiment",
    )

    if not db.get_experiment(d["experiment_id"]):
        db.add_experiment(
            d["experiment_id"],
            type_id=exp_type_id,
            sample_id=d["sample_id"],
            project_id=d["project_id"],
            measured_by_user_id=d["user_id"],
            measured_by_device_id=d["device_id"],
            started_at=now,
            status="completed",
            details="Synthetic burst-table selection demonstration; instrument and sample are demo context",
        )
        logger.info("Created experiment %s", d["experiment_id"])

    return probe_a488, probe_a647, pos_a488, pos_a647


def _demo_tables(
    data_dir: str | Path | None,
) -> tuple[Path | Traversable, list[tuple[str, bytes, list[dict[str, str]]]]]:
    """Read and validate all packaged or explicitly supplied tables before any write."""
    source = Path(data_dir) if data_dir is not None else files("mmfdb").joinpath("data").joinpath("demo")
    tables = []
    for filename in DEMO_FILES:
        try:
            payload = source.joinpath(filename).read_bytes()
            reader = csv.DictReader(io.StringIO(payload.decode("utf-8")), strict=True)
            if reader.fieldnames != list(DEMO_COLUMNS):
                raise ValueError("expected burst_id,size,proximity_ratio columns")
            rows = list(reader)
            if not rows:
                raise ValueError("table has no rows")
            identities = set()
            for row in rows:
                if set(row) != set(DEMO_COLUMNS) or any(value is None for value in row.values()):
                    raise ValueError("invalid table row")
                identity, size, ratio = (
                    int(row["burst_id"]),
                    int(row["size"]),
                    float(row["proximity_ratio"]),
                )
                if (
                    identity < 0
                    or identity in identities
                    or size <= 0
                    or not math.isfinite(ratio)
                    or not 0 <= ratio <= 1
                ):
                    raise ValueError("invalid burst-table values")
                identities.add(identity)
            tables.append((filename, payload, rows))
        except (OSError, UnicodeError, csv.Error, TypeError, ValueError) as error:
            raise ValueError(f"invalid demo table {filename}: {error}") from error
    return source, tables


def _register_demo_table(
    db: MFDatabase,
    artifact_id: str,
    filename: str,
    payload: bytes,
    row_count: int,
    columns: tuple[str, ...],
    *,
    valid: bool = True,
) -> None:
    """Store real table bytes once and attach a truthful content-addressed artifact."""
    if db.get_artifact(artifact_id) is not None:
        return
    reference = db.put_object(data=payload, filename=filename, mime_type="text/csv")
    db.register_artifact(
        artifact_id=artifact_id,
        artifact_kind="burst_table",
        data_format="csv",
        storage_mode="managed_archive",
        experiment_id=DEMO["experiment_id"],
        object_uuid=reference["object_uuid"],
        size_bytes=len(payload),
        row_count=row_count,
        checksum=hashlib.sha256(payload).hexdigest(),
        checksum_algorithm="sha256",
        mime_type="text/csv",
        validation_status="valid" if valid else "unvalidated",
        metadata={
            "synthetic": True,
            "filename": filename,
            "columns": list(columns),
            "description": "MMFDB synthetic burst-table demonstration",
        },
    )


def seed_example(
    db_path: str | Path | None = None, *, data_dir: str | Path | None = None
) -> dict[str, object]:
    """Populate demo metadata and a real synthetic burst-table selection lineage.

    Parameters
    ----------
    db_path : str or pathlib.Path, optional
        Target database; defaults to the configured database.
    data_dir : str or pathlib.Path, optional
        Directory containing the three named demo CSV tables. Defaults to
        packaged MMFDB resources. All inputs are validated before writes.

    Returns
    -------
    dict
        Demo IDs, source names and measured input/output row counts. Historical
        raw_data_ids and used_test_files keys remain aliases for table inputs.
    """
    source, tables = _demo_tables(data_dir)
    path = db_path if db_path is not None else resolve_database_path()
    raw_ids = [f"raw_demo_sm_dna_{index:03d}" for index in range(len(tables))]
    quality_id = "raw_demo_unlinked_sample_red_flag"
    product_id = f"prod_{DEMO['processing_id']}"
    output_columns = ("artifact_id", *DEMO_COLUMNS)
    output_rows = [
        {"artifact_id": artifact_id, **row}
        for artifact_id, (_, _, rows) in zip(raw_ids, tables)
        for row in rows
        if float(row["proximity_ratio"]) >= SELECTION_MIN
    ]
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=output_columns)
    writer.writeheader()
    writer.writerows(output_rows)
    output = stream.getvalue().encode("utf-8")
    expected = [
        (artifact_id, payload, len(rows))
        for artifact_id, (_, payload, rows) in zip(raw_ids, tables)
    ]
    expected.extend(
        [(quality_id, tables[0][1], len(tables[0][2])), (product_id, output, len(output_rows))]
    )
    with MFDatabase(path) as db:
        for artifact_id, payload, row_count in expected:
            existing = db.get_artifact(artifact_id)
            if existing is not None and (
                existing["checksum"] != hashlib.sha256(payload).hexdigest()
                or existing["row_count"] != row_count
                or not existing["object_uuid"]
                or db.get_object(existing["object_uuid"]) != payload
            ):
                raise ValueError(
                    f"demo artifact {artifact_id} is already seeded from different data"
                )
        _seed_flr_tables(db)
        with db._transaction():
            for artifact_id, (filename, payload, rows) in zip(raw_ids, tables):
                _register_demo_table(db, artifact_id, filename, payload, len(rows), DEMO_COLUMNS)
                link_artifact_to_sample(db, artifact_id, DEMO["sample_id"])
            _register_demo_table(
                db,
                quality_id,
                tables[0][0],
                tables[0][1],
                len(tables[0][2]),
                DEMO_COLUMNS,
                valid=False,
            )
            _register_demo_table(
                db, product_id, "selected_bursts.csv", output, len(output_rows), output_columns
            )
            link_artifact_to_sample(db, product_id, DEMO["sample_id"])
            if db.get_operation(DEMO["processing_id"]) is None:
                now = datetime.now(timezone.utc).isoformat()
                db.record_operation(
                    operation_id=DEMO["processing_id"],
                    operation_type="burst_filtering",
                    experiment_id=DEMO["experiment_id"],
                    operator_user_id=DEMO["user_id"],
                    settings={
                        "column": "proximity_ratio",
                        "minimum": SELECTION_MIN,
                        "input_rows": sum(len(rows) for _, _, rows in tables),
                        "output_rows": len(output_rows),
                        "synthetic": True,
                    },
                    software_package="mmfdb",
                    software_module="mmfdb.admin.seed_example",
                    status="succeeded",
                    started_at=now,
                    ended_at=now,
                )
                for ordinal, artifact_id in enumerate(raw_ids):
                    db.record_operation_link(
                        DEMO["processing_id"],
                        artifact_id,
                        "input",
                        role="burst_table",
                        ordinal=ordinal,
                    )
                db.record_operation_link(
                    DEMO["processing_id"], product_id, "output", role="burst_table"
                )
    source_files = [str(source.joinpath(filename)) for filename, _, _ in tables]
    return {
        "database_path": str(path),
        "sample_id": DEMO["sample_id"],
        "experiment_id": DEMO["experiment_id"],
        "processing_id": DEMO["processing_id"],
        "source_data_dir": str(source),
        "source_files": source_files,
        "used_test_files": source_files,
        "raw_data_ids": raw_ids,
        "quality_example_raw_data_ids": [quality_id],
        "processed_data_id": product_id,
        "input_row_counts": [len(rows) for _, _, rows in tables],
        "output_row_count": len(output_rows),
        "synthetic": True,
    }


if __name__ == "__main__":
    seed_example()
