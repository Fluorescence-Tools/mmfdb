"""Move chemicals and instruments from an ELN into MMFDB, and bundles out.

Identity is the ELN's: an imported object's MMFDB id is
``eln:<backend>:<kind>:<remote id>``, so a second pull updates the row it made
rather than adding a duplicate. Chemicals become reagent lots
(``mmfdb_reagent_lot``) and instruments become an instrument with a setup
(``flr_instrument`` / ``mmfdb_setup``); the DOI, InChIKey, SMILES and CAS number
the ELN holds travel in the row's text fields. The ELN is the authority for
inventory and chemical identity, so a pull overwrites those fields.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mmfdb.eln.model import ElnAttachment, ElnRecord, ExternalRef


def local_id(backend: str, kind: str, remote_id: str) -> str:
    """Return the MMFDB id of an object that lives in an ELN."""
    return f"eln:{backend}:{kind}:{remote_id}"


def pull(gateway: Any, db: Any, kinds: tuple[str, ...] = ("chemicals", "instruments")) -> dict[str, int]:
    """Import chemicals and instruments; return ``{"chemicals": n, "instruments": n}``.

    Parameters
    ----------
    gateway : ChemotionGateway
        Any gateway with ``chemicals()`` and ``instruments()``.
    db : MFDatabase
        Open database.
    kinds : tuple of str
        What to import.

    Returns
    -------
    dict
        Counts of objects created or updated.
    """
    from mmfdb.security.session import configured_default_user_id

    owner = configured_default_user_id()
    if db.conn.execute("SELECT 1 FROM flr_sample_users WHERE user_id = ?", (owner,)).fetchone() is None:
        db.add_user(owner, owner)
    report = {kind: 0 for kind in kinds}
    if "chemicals" in kinds:
        for chem in gateway.chemicals():
            _upsert_lot(db, gateway.backend, chem, owner)
            report["chemicals"] += 1
    if "instruments" in kinds:
        for inst in gateway.instruments():
            _upsert_instrument(db, gateway.backend, inst, owner)
            report["instruments"] += 1
    return report


def _upsert_lot(db: Any, backend: str, chem: Any, owner: str) -> None:
    from mmfdb.samples.reagents import add_reagent_lot

    lot_id = local_id(backend, "chemical", chem.remote_id)
    identity = "; ".join(f"{k}={v}" for k, v in (
        ("InChIKey", chem.inchikey), ("SMILES", chem.smiles), ("CAS", chem.cas),
        ("formula", chem.formula), ("source", f"{backend}:{chem.remote_id}")) if v)
    with db.transaction():
        db.conn.execute("DELETE FROM mmfdb_reagent_usage WHERE lot_id = ?", (lot_id,))
        db.conn.execute("DELETE FROM mmfdb_reagent_lot WHERE lot_id = ?", (lot_id,))
        add_reagent_lot(
            db, kind="other", name=chem.name or chem.remote_id, lot_number=chem.lot_number,
            vendor=chem.vendor, catalog_no=chem.cas, details=identity, lot_id=lot_id,
            created_by_user_id=owner,
        )


def _upsert_instrument(db: Any, backend: str, inst: Any, owner: str) -> None:
    instrument_id = local_id(backend, "instrument", inst.remote_id)
    details = "; ".join(f"{k}={v}" for k, v in (
        ("DOI", inst.doi), ("manufacturer", inst.manufacturer), ("model", inst.model),
        ("serial", inst.serial_number), ("source", f"{backend}:{inst.remote_id}")) if v)
    with db.transaction():
        db.dao.upsert("flr_instrument", {
            "instrument_id": instrument_id, "instrument_name": inst.name or inst.remote_id,
            "details": details, "deleted_at": None})
    db.save_setup(
        local_id(backend, "setup", inst.remote_id), inst.name or inst.remote_id,
        instrument_id=instrument_id, description=inst.description or details,
        created_by_user_id=owner,
    )


def deposit_pto(gateway: Any, pto_path: str | Path, *, title: str | None = None) -> ExternalRef:
    """Deposit a ``.pto`` / ``.cs.pto`` and its mmCIF provenance as one ELN record.

    Parameters
    ----------
    gateway : ChemotionGateway
        Destination.
    pto_path : str or Path
        The container, sent byte for byte.
    title : str, optional
        Record title; the file name by default.

    Returns
    -------
    ExternalRef
        The created record.
    """
    from mmfdb.provenance.pto_graph import graph_from_pto, write_graph_cif

    path = Path(pto_path)
    graph = graph_from_pto(path)
    cif = write_graph_cif(graph)
    record = ElnRecord(
        title=title or path.name,
        description=(f"MMFDB export of {path.name}: {len(graph['operations'])} operations, "
                     f"{len(graph['artifacts'])} artifacts. Provenance is in the attached mmCIF."),
        metadata={"format": "mmfdb.provenance.cif", "operations": len(graph["operations"]),
                  "artifacts": len(graph["artifacts"])},
    )
    return gateway.deposit(record, [
        ElnAttachment(path.name, path.read_bytes()),
        ElnAttachment(path.stem + ".provenance.cif", cif.encode("utf-8"), "chemical/x-mmcif"),
    ])
