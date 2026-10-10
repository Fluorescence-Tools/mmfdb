"""Gateway to a Chemotion ELN (REST ``/api/v1``, bearer token).

Read path: samples (with their molecule: InChIKey, SMILES, formula) and devices.
Deposit path: a research plan carrying the files of an MMFDB export bundle.

The response shapes parsed here are pinned by the recorded fixtures in
``tests/fixtures/chemotion/``; they follow the public Chemotion ELN API as
documented at ``/api/v1/swagger_doc`` and were not captured from a live server.
Before the first live use, replay one list call and compare it with the fixture:
the parsers tolerate absent fields (they become empty strings) but an endpoint
that moved fails loudly as :class:`ElnUnavailable`, never as silently empty data.
"""

from __future__ import annotations

import json
import urllib.parse
from collections.abc import Iterator
from typing import Any

from mmfdb.eln.http import Request, Response, Transport, multipart, urllib_transport
from mmfdb.eln.model import (
    ElnAttachment,
    ElnChemical,
    ElnInstrument,
    ElnRecord,
    ElnUnavailable,
    ExternalRef,
)

BACKEND = "chemotion"
PAGE_SIZE = 50
MAX_PAGES = 200


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def parse_chemical(sample: dict[str, Any]) -> ElnChemical:
    """Translate one Chemotion sample into the neutral chemical."""
    molecule = sample.get("molecule") or {}
    xref = sample.get("xref") or {}
    weight = molecule.get("molecular_weight")
    return ElnChemical(
        remote_id=_text(sample.get("id")),
        name=_text(sample.get("name") or sample.get("short_label")),
        inchikey=_text(molecule.get("inchikey")),
        smiles=_text(molecule.get("cano_smiles")),
        cas=_text(xref.get("cas") if isinstance(xref, dict) else ""),
        formula=_text(molecule.get("sum_formular")),
        molecular_weight=float(weight) if isinstance(weight, (int, float)) else None,
        vendor=_text(sample.get("supplier")),
        lot_number=_text(sample.get("external_label")),
        description=_text(sample.get("description")),
        modified_at=_text(sample.get("updated_at")),
    )


def parse_instrument(device: dict[str, Any]) -> ElnInstrument:
    """Translate one Chemotion device into the neutral instrument."""
    meta = device.get("device_metadata") or {}
    makers = meta.get("manufacturers") or []
    maker = makers[0].get("name") if makers and isinstance(makers[0], dict) else ""
    return ElnInstrument(
        remote_id=_text(device.get("id")),
        name=_text(device.get("name")),
        doi=_text(meta.get("doi")),
        manufacturer=_text(maker or device.get("vendor_name")),
        model=_text(device.get("model") or device.get("device_type")),
        serial_number=_text(device.get("serial_number")),
        description=_text(device.get("description")),
        modified_at=_text(device.get("updated_at")),
    )


class ChemotionGateway:
    """Read chemicals and instruments from, and deposit bundles into, a Chemotion ELN.

    Parameters
    ----------
    base_url : str
        Server root, ``https://eln.example`` (``/api/v1`` is appended).
    token : str
        Bearer token. Held in memory, hidden from ``repr``, sent only in the
        ``Authorization`` header.
    collection_id : int, optional
        Collection a deposit goes into, and the scope of the read calls.
    transport : callable, optional
        Replaces the network, for tests.
    allow_insecure_http : bool
        Permit ``http://`` (a local test server only).
    """

    backend = BACKEND

    def __init__(self, base_url: str, token: str, *, collection_id: int | None = None,
                 timeout: float = 20.0, verify_tls: bool = True,
                 allow_insecure_http: bool = False, transport: Transport | None = None) -> None:
        """Validate the endpoint and keep the credential out of every representation."""
        parts = urllib.parse.urlsplit(str(base_url).strip())
        if parts.scheme not in {"https", "http"} or not parts.hostname:
            raise ValueError("Chemotion endpoint must be an http(s) URL with a host")
        if parts.scheme == "http" and not allow_insecure_http:
            raise ValueError("Chemotion endpoint must use HTTPS unless explicitly allowed")
        if parts.username is not None or parts.password is not None or parts.query or parts.fragment:
            raise ValueError("Chemotion endpoint must not contain credentials, a query or a fragment")
        if not str(token or "").strip():
            raise ValueError("Chemotion token is required")
        self.root = f"{parts.scheme}://{parts.netloc}{parts.path.rstrip('/')}"
        self.api = self.root + "/api/v1"
        self._token = str(token).strip()
        self.collection_id = collection_id
        self.timeout = float(timeout)
        self.verify_tls = bool(verify_tls)
        self._transport = transport or urllib_transport

    def __repr__(self) -> str:
        """Return the endpoint, never the token."""
        return f"ChemotionGateway(root={self.root!r}, collection_id={self.collection_id!r})"

    def capabilities(self) -> frozenset[str]:
        """Name what this backend supports: chemicals, instruments, deposit, doi."""
        return frozenset({"chemicals", "instruments", "deposit", "doi"})

    # -- read -------------------------------------------------------------------

    def chemicals(self) -> Iterator[ElnChemical]:
        """Yield every sample of the collection as a neutral chemical."""
        for sample in self._pages("/samples.json", "samples"):
            yield parse_chemical(sample)

    def instruments(self) -> Iterator[ElnInstrument]:
        """Yield every device as a neutral instrument."""
        for device in self._pages("/devices", "devices"):
            yield parse_instrument(device)

    def _pages(self, path: str, key: str) -> Iterator[dict[str, Any]]:
        for page in range(1, MAX_PAGES + 1):
            query = {"per_page": PAGE_SIZE, "page": page}
            if self.collection_id is not None:
                query["collection_id"] = self.collection_id
            payload, _ = self._call("GET", path, query=query, expected={200})
            rows = payload.get(key, []) if isinstance(payload, dict) else payload
            if not isinstance(rows, list):
                raise ElnUnavailable(f"unexpected response shape from {path}")
            yield from (r for r in rows if isinstance(r, dict))
            if len(rows) < PAGE_SIZE:
                return
        raise ElnUnavailable(f"{path} did not end within {MAX_PAGES} pages")

    # -- deposit ----------------------------------------------------------------

    def deposit(self, record: ElnRecord, attachments: list[ElnAttachment]) -> ExternalRef:
        """Create a research plan for *record* and attach every file to it.

        Nothing is half-done silently: a failed attachment raises
        :class:`ElnUnavailable` and the error names the created research plan, so
        it can be removed or completed in the ELN.

        Parameters
        ----------
        record : ElnRecord
            Title, description and metadata of the record.
        attachments : list of ElnAttachment
            Files sent verbatim.

        Returns
        -------
        ExternalRef
            The created research plan.
        """
        body: dict[str, Any] = {"name": record.title, "description": record.description}
        if self.collection_id is not None:
            body["collection_id"] = self.collection_id
        if record.metadata:
            body["metadata"] = record.metadata
        payload, _ = self._call("POST", "/research_plans", json_body=body, expected={200, 201})
        plan = payload.get("research_plan", payload) if isinstance(payload, dict) else {}
        plan_id = plan.get("id")
        if plan_id is None:
            raise ElnUnavailable("the ELN created no research plan id")
        ref = ExternalRef(BACKEND, "research_plan", str(plan_id),
                          f"{self.root}/mydb/collection/all/research_plan/{plan_id}")
        for item in attachments:
            data, content_type = multipart(
                {"attachable_id": str(plan_id), "attachable_type": "ResearchPlan"},
                {"file": (item.name, item.data, item.media_type)},
            )
            try:
                self._call("POST", "/attachments", raw_body=data, content_type=content_type,
                           expected={200, 201})
            except ElnUnavailable as exc:
                raise ElnUnavailable(
                    f"attachment {item.name!r} failed after research plan {plan_id} was created: {exc}"
                ) from exc
        return ref

    # -- transport --------------------------------------------------------------

    def _call(self, method: str, path: str, *, query: dict[str, Any] | None = None,
              json_body: dict[str, Any] | None = None, raw_body: bytes | None = None,
              content_type: str = "", expected: set[int]) -> tuple[Any, Response]:
        url = self.api + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        headers = {"Accept": "application/json", "Authorization": f"Bearer {self._token}",
                   "User-Agent": "mmfdb-eln/1"}
        body = raw_body
        if json_body is not None:
            body = json.dumps(json_body, separators=(",", ":")).encode("utf-8")
            content_type = "application/json"
        if content_type:
            headers["Content-Type"] = content_type
        response = self._transport(Request(method, url, headers, body, self.timeout, self.verify_tls))
        if int(response.status) not in expected:
            raise ElnUnavailable(f"{method} {path}: HTTP {response.status}")
        if not response.body:
            return None, response
        try:
            return json.loads(response.body.decode("utf-8")), response
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ElnUnavailable(f"{method} {path}: malformed JSON") from exc
