"""Neutral entities every ELN backend is translated to and from.

MMFDB speaks to an electronic lab notebook through these few dataclasses and
nothing else, so a second backend (eLabFTW, an institutional one) is another
gateway module, not another data model. Only what MMFDB stores is modelled:
chemicals, instruments, and the record a deposit creates.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class ElnUnavailable(RuntimeError):
    """The ELN could not be reached or refused the request; nothing was written."""


@dataclass(frozen=True)
class ExternalRef:
    """Where an object lives in an ELN."""

    backend: str
    kind: str
    remote_id: str
    url: str = ""


@dataclass(frozen=True)
class ElnChemical:
    """A chemical or sample with the identifiers that make it the same substance elsewhere."""

    remote_id: str
    name: str
    inchikey: str = ""
    smiles: str = ""
    cas: str = ""
    formula: str = ""
    molecular_weight: float | None = None
    vendor: str = ""
    lot_number: str = ""
    description: str = ""
    modified_at: str = ""


@dataclass(frozen=True)
class ElnInstrument:
    """A device record, with the DOI that makes it citable when the ELN has one."""

    remote_id: str
    name: str
    doi: str = ""
    manufacturer: str = ""
    model: str = ""
    serial_number: str = ""
    description: str = ""
    modified_at: str = ""


@dataclass(frozen=True)
class ElnAttachment:
    """A file travelling with a deposit; the bytes are sent verbatim."""

    name: str
    data: bytes = field(repr=False)
    media_type: str = "application/octet-stream"


@dataclass(frozen=True)
class ElnRecord:
    """What a deposit creates: a titled record carrying attachments."""

    title: str
    description: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
