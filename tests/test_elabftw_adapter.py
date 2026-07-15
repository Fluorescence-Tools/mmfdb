"""Production contracts for the dependency-free eLabFTW adapter."""

from __future__ import annotations

import json

import pytest
from mmfdb.adapters.elabftw import (
    ELabFTWClient,
    ELabFTWHTTPError,
    ELabFTWResponse,
    ELabFTWTransportError,
    mmfdb_experiment_id,
    remote_to_mmfdb_experiment,
)


class FakeTransport:
    """Record deterministic adapter requests and return queued responses."""

    def __init__(self, responses: list[ELabFTWResponse]):
        """Initialize the response queue."""
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("unexpected HTTP request")
        return self.responses.pop(0)


def response(status: int, payload=None, **headers) -> ELabFTWResponse:
    body = b"" if payload is None else json.dumps(payload).encode("utf-8")
    return ELabFTWResponse(status=status, headers=headers, body=body)


def test_client_normalizes_endpoint_and_never_exposes_key() -> None:
    transport = FakeTransport([response(200, {"version": "5.2"})])
    client = ELabFTWClient(
        "https://lab.example/",
        "top-secret-key",
        transport=transport,
    )

    assert client.info() == {"version": "5.2"}
    request = transport.requests[0]
    assert request.url == "https://lab.example/api/v2/info"
    assert request.headers["Authorization"] == "top-secret-key"
    assert "top-secret-key" not in repr(client)
    assert "top-secret-key" not in repr(request)


@pytest.mark.parametrize(
    "url,kwargs,message",
    [
        ("http://lab.example", {}, "HTTPS"),
        ("ftp://lab.example", {}, "HTTPS"),
        ("https://user:pw@lab.example", {}, "credentials"),
        ("https://lab.example?key=secret", {}, "query"),
        ("https://lab.example", {"timeout": 0}, "timeout"),
        ("https://lab.example", {"timeout": 121}, "timeout"),
    ],
)
def test_client_rejects_unsafe_or_unbounded_configuration(url, kwargs, message) -> None:
    with pytest.raises(ValueError, match=message):
        ELabFTWClient(url, "key", **kwargs)


def test_plain_http_requires_explicit_opt_in() -> None:
    client = ELabFTWClient(
        "http://elab.internal",
        "key",
        allow_insecure_http=True,
        transport=FakeTransport([]),
    )
    assert client.endpoint == "http://elab.internal/api/v2"


def test_list_experiments_paginates_with_bounds_and_deduplicates() -> None:
    transport = FakeTransport(
        [
            response(200, [{"id": 1}, {"id": 2}]),
            response(200, [{"id": 2}, {"id": 3}]),
            response(200, []),
        ]
    )
    client = ELabFTWClient("https://lab.example", "key", transport=transport)

    rows = client.list_experiments(page_size=2, max_items=10)

    assert [row["id"] for row in rows] == [1, 2, 3]
    assert "limit=2&offset=0" in transport.requests[0].url
    assert "limit=2&offset=2" in transport.requests[1].url
    assert "limit=2&offset=4" in transport.requests[2].url


def test_list_experiments_rejects_malformed_response_and_stalled_pages() -> None:
    malformed = ELabFTWClient(
        "https://lab.example", "key", transport=FakeTransport([response(200, {})])
    )
    with pytest.raises(ELabFTWTransportError, match="JSON array"):
        malformed.list_experiments()

    stalled = ELabFTWClient(
        "https://lab.example",
        "key",
        transport=FakeTransport(
            [response(200, [{"id": 1}]), response(200, [{"id": 1}])]
        ),
    )
    with pytest.raises(ELabFTWTransportError, match="no progress"):
        stalled.list_experiments(page_size=1)


def test_http_error_is_typed_bounded_and_does_not_leak_key() -> None:
    transport = FakeTransport(
        [response(401, {"message": "bad key", "description": "denied"})]
    )
    client = ELabFTWClient(
        "https://lab.example", "never-print-me", transport=transport
    )

    with pytest.raises(ELabFTWHTTPError) as error:
        client.info()

    assert error.value.status == 401
    assert "bad key" in str(error.value)
    assert "never-print-me" not in str(error.value)


def test_custom_transport_cannot_bypass_response_size_limit() -> None:
    client = ELabFTWClient(
        "https://lab.example",
        "key",
        max_response_bytes=4,
        transport=FakeTransport([ELabFTWResponse(200, {}, b"12345")]),
    )
    with pytest.raises(ELabFTWTransportError, match="safety limit"):
        client.info()


def test_create_uses_location_header_and_patch_uses_json() -> None:
    transport = FakeTransport(
        [
            response(
                201,
                None,
                location="https://lab.example/api/v2/experiments/42",
            ),
            response(200, {"id": 42, "title": "Updated"}),
        ]
    )
    client = ELabFTWClient("https://lab.example", "key", transport=transport)

    assert client.create_experiment({"title": "Created"}) == 42
    assert client.update_experiment(42, {"title": "Updated"})["title"] == "Updated"
    assert transport.requests[0].method == "POST"
    assert json.loads(transport.requests[0].body) == {"title": "Created"}
    assert transport.requests[1].method == "PATCH"


def test_remote_mapping_has_stable_identity_and_preserves_local_details() -> None:
    remote = {
        "id": 17,
        "title": "Lifetime series",
        "body": "<p>measurement notes</p>",
        "date": "2026-07-13",
        "status_title": "Running",
        "metadata": '{"extra_fields": {}}',
        "tags": ["tcspc", "fret"],
        "modified_at": "2026-07-13 12:00:00",
    }
    existing = {"details": json.dumps({"local_note": "keep me"})}

    mapped = remote_to_mmfdb_experiment(
        remote,
        endpoint="https://lab.example/api/v2",
        owner_user_id="alice",
        type_id=3,
        existing=existing,
    )

    assert mapped["experiment_id"] == mmfdb_experiment_id(
        "https://lab.example/api/v2", 17
    )
    assert mapped["measured_by_user_id"] == "alice"
    assert mapped["type_id"] == 3
    details = json.loads(mapped["details"])
    assert details["local_note"] == "keep me"
    assert details["elabftw_source"]["remote_id"] == 17
    assert details["elabftw_source"]["title"] == "Lifetime series"
    assert details["elabftw_source"]["metadata"] == {"extra_fields": {}}


def test_mapping_rejects_missing_or_invalid_remote_ids() -> None:
    for remote in ({}, {"id": 0}, {"id": "not-an-int"}, {"id": True}):
        with pytest.raises(ValueError, match="positive integer"):
            remote_to_mmfdb_experiment(
                remote,
                endpoint="https://lab.example/api/v2",
                owner_user_id="alice",
                type_id=1,
            )
