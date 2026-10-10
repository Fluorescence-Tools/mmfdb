"""The Chemotion gateway against recorded responses: no socket is ever opened.

The fixtures in ``fixtures/chemotion/`` follow the shapes of the public Chemotion
ELN API; they are replayed through the gateway's injectable transport.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mmfdb.eln import ChemotionGateway, ElnUnavailable, deposit_pto, local_id, pull
from mmfdb.eln.http import Request, Response
from mmfdb.repository import MFDatabase

FIXTURES = Path(__file__).with_name("fixtures") / "chemotion"
TOKEN = "s3cret-token"


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


class Recorded:
    """Replay routes in order and keep every request for inspection."""

    def __init__(self, routes: dict[tuple[str, str], list[Response]]):
        self.routes = {k: list(v) for k, v in routes.items()}
        self.requests: list[Request] = []

    def __call__(self, request: Request) -> Response:
        self.requests.append(request)
        path = request.url.split("?")[0].split("/api/v1")[1]
        queue = self.routes.get((request.method, path))
        if not queue:
            raise AssertionError(f"unexpected request {request.method} {path}")
        return queue.pop(0) if len(queue) > 1 else queue[0]


def ok(name: str, status: int = 200) -> Response:
    return Response(status, {}, _fixture(name))


@pytest.fixture
def transport() -> Recorded:
    return Recorded({
        ("GET", "/samples.json"): [ok("samples_page1.json")],
        ("GET", "/devices"): [ok("devices_page1.json")],
        ("POST", "/research_plans"): [ok("research_plan_created.json", 201)],
        ("POST", "/attachments"): [ok("attachment_created.json", 201)],
    })


def gateway(transport, **kw) -> ChemotionGateway:
    return ChemotionGateway("https://eln.example", TOKEN, transport=transport, **kw)


def test_the_token_is_sent_as_a_bearer_and_never_shown(transport):
    g = gateway(transport, collection_id=4)
    list(g.chemicals())
    request = transport.requests[0]
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert request.url.startswith("https://eln.example/api/v1/samples.json?")
    assert "collection_id=4" in request.url
    assert TOKEN not in repr(g) and TOKEN not in repr(request)


@pytest.mark.parametrize("url", ["http://eln.example", "https://u:p@eln.example",
                                 "https://eln.example?x=1", "ftp://eln.example"])
def test_unsafe_endpoints_are_refused(url, transport):
    with pytest.raises(ValueError):
        ChemotionGateway(url, TOKEN, transport=transport)


def test_samples_become_neutral_chemicals(transport):
    first, second = gateway(transport).chemicals()
    assert (first.remote_id, first.cas, first.formula) == ("101", "247144-96-5", "C9H8O4")
    assert first.inchikey == "WBQSBZKOGNPJGR-UHFFFAOYSA-N" and first.lot_number == "LOT-8841"
    assert second.cas == "" and second.vendor == ""


def test_devices_become_neutral_instruments_with_their_doi(transport):
    (device,) = gateway(transport).instruments()
    assert (device.name, device.doi, device.manufacturer) == (
        "Microtime 200", "10.5072/example.mt200", "PicoQuant")


def test_pull_imports_reagents_and_a_setup_and_is_idempotent(transport, tmp_path):
    with MFDatabase(str(tmp_path / "eln.sqlite")) as db:
        for _ in range(2):  # a second pull updates, never duplicates
            assert pull(gateway(transport), db) == {"chemicals": 2, "instruments": 1}
        lots = [dict(r) for r in db.conn.execute(
            "SELECT * FROM mmfdb_reagent_lot WHERE deleted_at IS NULL ORDER BY lot_id")]
        assert [r["lot_id"] for r in lots] == [local_id("chemotion", "chemical", "101"),
                                               local_id("chemotion", "chemical", "102")]
        assert lots[0]["catalog_no"] == "247144-96-5"
        assert "InChIKey=WBQSBZKOGNPJGR-UHFFFAOYSA-N" in lots[0]["details"]
        setup = db.get_setup(local_id("chemotion", "setup", "3"))
        assert setup["name"] == "Microtime 200"
        instrument = db.conn.execute("SELECT details FROM flr_instrument WHERE instrument_id = ?",
                                     (local_id("chemotion", "instrument", "3"),)).fetchone()
        assert "DOI=10.5072/example.mt200" in instrument[0]


def test_a_failing_request_raises_and_writes_nothing(tmp_path):
    bad = Recorded({("GET", "/samples.json"): [Response(401, {}, b'{"error":"unauthorized"}')]})
    with MFDatabase(str(tmp_path / "eln.sqlite")) as db, pytest.raises(ElnUnavailable, match="401"):
        pull(gateway(bad), db, kinds=("chemicals",))
    with MFDatabase(str(tmp_path / "eln.sqlite")) as db:
        assert db.conn.execute("SELECT count(*) FROM mmfdb_reagent_lot").fetchone()[0] == 0


def test_deposit_sends_the_container_byte_for_byte_with_its_provenance(transport, tmp_path):
    tttrlib = pytest.importorskip("tttrlib")
    path = tmp_path / "m000.pto"
    h = tttrlib.PtoFile()
    assert h.create(str(path), "t")
    raw = h.add("tttr_photon_stream", "ptu", "m000.ptu", b"photons" * 8)
    tttrlib.pto_describe(h, raw, data_format="ptu", software="chisurf 9")
    table = h.add("burst_table", "raw", "bursts", b"x" * 8)
    tttrlib.pto_describe(h, table, data_format="dstore", row_grain="burst",
                         operation_type="burst_selection", parameters={"m": 1},
                         derived_from=[raw], software="chisurf 9")
    assert h.commit()
    h.close()

    ref = deposit_pto(gateway(transport, collection_id=4), path)
    assert (ref.backend, ref.kind, ref.remote_id) == ("chemotion", "research_plan", "55")
    plan, first, second = transport.requests
    assert json.loads(plan.body)["collection_id"] == 4
    assert path.read_bytes() in first.body              # the container, verbatim
    assert b"_mmfdb_provenance_operation" in second.body  # and its provenance as mmCIF
    assert first.headers["Content-Type"].startswith("multipart/form-data; boundary=")


def test_a_failed_attachment_names_the_record_it_left_behind(tmp_path):
    from mmfdb.eln import ElnAttachment, ElnRecord

    bad = Recorded({("POST", "/research_plans"): [ok("research_plan_created.json", 201)],
                    ("POST", "/attachments"): [Response(500, {}, b"")]})
    with pytest.raises(ElnUnavailable, match="research plan 55"):
        gateway(bad).deposit(ElnRecord("t"), [ElnAttachment("a.bin", b"1")])


def test_tokens_use_the_credential_store_then_the_environment(monkeypatch):
    from mmfdb.eln import credentials
    from mmfdb.security import credentials as store

    kept: dict = {}
    monkeypatch.setattr(store, "store_session_token", lambda h, p, u, t: kept.update({(h, p, u): t}) or True)
    monkeypatch.setattr(store, "load_session_token", lambda h, p, u: kept.get((h, p, u)))
    assert credentials.store_token("chemotion", "https://eln.example/x", "me", "tok")
    assert credentials.load_token("chemotion", "https://eln.example", "me") == "tok"
    assert credentials.load_token("chemotion", "https://eln.example", "you") is None
    monkeypatch.setenv("MMFDB_CHEMOTION_TOKEN", "from-env")
    assert credentials.load_token("chemotion", "https://eln.example", "you") == "from-env"


def test_the_command_line_pulls_with_a_stored_token(monkeypatch, tmp_path, transport):
    from click.testing import CliRunner

    from mmfdb import cli as cli_module
    from mmfdb.eln import chemotion

    monkeypatch.setenv("MMFDB_CHEMOTION_TOKEN", TOKEN)
    monkeypatch.setattr(chemotion, "urllib_transport", transport)
    db_path = tmp_path / "cli.sqlite"
    result = CliRunner().invoke(cli_module.cli, ["eln", "pull", "--url", "https://eln.example",
                                                 "--database", str(db_path)])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {"chemicals": 2, "instruments": 1}
    missing = CliRunner().invoke(cli_module.cli, ["eln", "pull", "--url", "https://x.example",
                                                  "--user", "nobody", "--database", str(db_path)],
                                 env={"MMFDB_CHEMOTION_TOKEN": ""})
    assert missing.exit_code != 0 and "no token" in missing.output
