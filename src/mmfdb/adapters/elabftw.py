"""Small, dependency-free adapter for the eLabFTW REST API v2.

The adapter deliberately owns only HTTP and lossless metadata mapping.  MMFDB
authorization, persistence, and GUI concerns stay at their respective layers.
API keys are held in memory, never included in object representations, and are
sent only in the ``Authorization`` header documented by eLabFTW.
"""

from __future__ import annotations

import hashlib
import html
import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

DEFAULT_TIMEOUT_SECONDS = 15.0
MAX_TIMEOUT_SECONDS = 120.0
DEFAULT_MAX_RESPONSE_BYTES = 10 * 1024 * 1024
MAX_PAGE_SIZE = 100
MAX_LIST_ITEMS = 10_000


class ELabFTWError(RuntimeError):
    """Base error for the eLabFTW adapter."""


class ELabFTWTransportError(ELabFTWError):
    """The remote response could not be transported or decoded safely."""


class ELabFTWHTTPError(ELabFTWError):
    """An eLabFTW endpoint returned a non-success status."""

    def __init__(self, status: int, message: str):
        """Record the remote status without retaining request credentials."""
        self.status = int(status)
        super().__init__(f"eLabFTW HTTP {self.status}: {message}")


@dataclass(frozen=True, slots=True)
class ELabFTWRequest:
    """One transport request with a secret-safe representation."""

    method: str
    url: str
    headers: Mapping[str, str]
    body: bytes | None
    timeout: float
    verify_tls: bool
    max_response_bytes: int

    def __repr__(self) -> str:
        """Return a representation with the authorization header redacted."""
        safe_headers = {
            key: "***REDACTED***" if key.lower() == "authorization" else value
            for key, value in self.headers.items()
        }
        return (
            f"ELabFTWRequest(method={self.method!r}, url={self.url!r}, "
            f"headers={safe_headers!r}, body_bytes={len(self.body or b'')}, "
            f"timeout={self.timeout!r}, verify_tls={self.verify_tls!r})"
        )


@dataclass(frozen=True, slots=True)
class ELabFTWResponse:
    """Transport-neutral HTTP response."""

    status: int
    headers: Mapping[str, str]
    body: bytes


ELabFTWTransport = Callable[[ELabFTWRequest], ELabFTWResponse]


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    """Prevent API credentials from following redirects to another origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def _read_bounded(stream: Any, limit: int) -> bytes:
    payload = stream.read(limit + 1)
    if len(payload) > limit:
        raise ELabFTWTransportError(
            f"eLabFTW response exceeds the {limit}-byte safety limit"
        )
    return payload


def _urllib_transport(request: ELabFTWRequest) -> ELabFTWResponse:
    req = urllib.request.Request(
        request.url,
        data=request.body,
        headers=dict(request.headers),
        method=request.method,
    )
    handlers: list[Any] = [_RejectRedirects()]
    if urllib.parse.urlsplit(request.url).scheme == "https":
        context = (
            ssl.create_default_context()
            if request.verify_tls
            else ssl._create_unverified_context()  # noqa: SLF001 - explicit opt-out
        )
        handlers.append(urllib.request.HTTPSHandler(context=context))
    opener = urllib.request.build_opener(*handlers)
    try:
        with opener.open(req, timeout=request.timeout) as response:
            return ELabFTWResponse(
                status=int(response.status),
                headers={key.lower(): value for key, value in response.headers.items()},
                body=_read_bounded(response, request.max_response_bytes),
            )
    except urllib.error.HTTPError as exc:
        try:
            body = _read_bounded(exc, request.max_response_bytes)
        finally:
            exc.close()
        return ELabFTWResponse(
            status=int(exc.code),
            headers={key.lower(): value for key, value in exc.headers.items()},
            body=body,
        )
    except ELabFTWTransportError:
        raise
    except (OSError, urllib.error.URLError) as exc:
        reason = getattr(exc, "reason", exc)
        raise ELabFTWTransportError(f"eLabFTW request failed: {reason}") from exc


def _normalize_endpoint(base_url: str, *, allow_insecure_http: bool) -> str:
    raw = str(base_url or "").strip()
    parsed = urllib.parse.urlsplit(raw)
    if parsed.scheme not in {"https", "http"}:
        raise ValueError("eLabFTW endpoint must use HTTPS")
    if parsed.scheme == "http" and not allow_insecure_http:
        raise ValueError("eLabFTW endpoint must use HTTPS unless explicitly allowed")
    if not parsed.hostname:
        raise ValueError("eLabFTW endpoint must include a host")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("eLabFTW endpoint must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("eLabFTW endpoint must not contain a query or fragment")

    path = parsed.path.rstrip("/")
    if not path.endswith("/api/v2"):
        path = f"{path}/api/v2"
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, path, "", "")
    )


def _positive_remote_id(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("eLabFTW remote id must be a positive integer")
    try:
        remote_id = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("eLabFTW remote id must be a positive integer") from exc
    if remote_id <= 0:
        raise ValueError("eLabFTW remote id must be a positive integer")
    return remote_id


class ELabFTWClient:
    """Synchronous eLabFTW REST v2 client with bounded I/O and pagination."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        verify_tls: bool = True,
        allow_insecure_http: bool = False,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        transport: ELabFTWTransport | None = None,
    ) -> None:
        """Configure a validated eLabFTW endpoint and bounded transport."""
        if not str(api_key or "").strip():
            raise ValueError("eLabFTW API key is required")
        timeout = float(timeout)
        if not 0 < timeout <= MAX_TIMEOUT_SECONDS:
            raise ValueError(
                "eLabFTW timeout must be greater than 0 and at most "
                f"{MAX_TIMEOUT_SECONDS:g} seconds"
            )
        max_response_bytes = int(max_response_bytes)
        if max_response_bytes <= 0:
            raise ValueError("eLabFTW response-size limit must be positive")
        self.endpoint = _normalize_endpoint(
            base_url, allow_insecure_http=bool(allow_insecure_http)
        )
        self._api_key = str(api_key).strip()
        self.timeout = timeout
        self.verify_tls = bool(verify_tls)
        self.max_response_bytes = max_response_bytes
        self._transport = transport or _urllib_transport

    def __repr__(self) -> str:
        """Return connection metadata without the API key."""
        return (
            f"ELabFTWClient(endpoint={self.endpoint!r}, timeout={self.timeout!r}, "
            f"verify_tls={self.verify_tls!r})"
        )

    def info(self) -> dict[str, Any]:
        """Return eLabFTW instance information from ``GET /info``."""
        payload, _ = self._request("GET", "/info", expected={200})
        if not isinstance(payload, dict):
            raise ELabFTWTransportError("eLabFTW /info did not return a JSON object")
        return payload

    def get_experiment(self, remote_id: int) -> dict[str, Any]:
        """Return one remote experiment by positive integer ID."""
        remote_id = _positive_remote_id(remote_id)
        payload, _ = self._request(
            "GET", f"/experiments/{remote_id}", expected={200}
        )
        if not isinstance(payload, dict):
            raise ELabFTWTransportError("eLabFTW experiment response is not a JSON object")
        return payload

    def list_experiments(
        self,
        *,
        query: str = "",
        page_size: int = 50,
        max_items: int = 500,
    ) -> list[dict[str, Any]]:
        """Return a bounded, deduplicated traversal of remote experiments."""
        page_size = int(page_size)
        max_items = int(max_items)
        if not 1 <= page_size <= MAX_PAGE_SIZE:
            raise ValueError(f"page_size must be between 1 and {MAX_PAGE_SIZE}")
        if not 1 <= max_items <= MAX_LIST_ITEMS:
            raise ValueError(f"max_items must be between 1 and {MAX_LIST_ITEMS}")

        rows: list[dict[str, Any]] = []
        seen: set[int] = set()
        offset = 0
        while len(rows) < max_items:
            limit = min(page_size, max_items - len(rows))
            payload, _ = self._request(
                "GET",
                "/experiments",
                query={"limit": limit, "offset": offset, "q": query or None},
                expected={200},
            )
            if not isinstance(payload, list):
                raise ELabFTWTransportError(
                    "eLabFTW experiments endpoint did not return a JSON array"
                )
            if not payload:
                break
            added = 0
            for item in payload:
                if not isinstance(item, dict):
                    raise ELabFTWTransportError(
                        "eLabFTW experiments array contains a non-object entry"
                    )
                remote_id = _positive_remote_id(item.get("id"))
                if remote_id not in seen:
                    seen.add(remote_id)
                    rows.append(item)
                    added += 1
                    if len(rows) >= max_items:
                        break
            if added == 0:
                raise ELabFTWTransportError(
                    "eLabFTW pagination made no progress; refusing an infinite loop"
                )
            offset += len(payload)
            if len(payload) < limit:
                break
        return rows

    def create_experiment(self, payload: Mapping[str, Any]) -> int:
        """Create a remote experiment and return its Location-derived ID."""
        result, headers = self._request(
            "POST", "/experiments", json_body=dict(payload), expected={200, 201}
        )
        if isinstance(result, dict) and result.get("id") is not None:
            return _positive_remote_id(result["id"])
        location = headers.get("location", "")
        remote_id = urllib.parse.urlsplit(location).path.rstrip("/").rsplit("/", 1)[-1]
        try:
            return _positive_remote_id(remote_id)
        except ValueError as exc:
            raise ELabFTWTransportError(
                "eLabFTW create response did not identify the new experiment"
            ) from exc

    def update_experiment(
        self, remote_id: int, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Explicitly update one remote experiment."""
        remote_id = _positive_remote_id(remote_id)
        result, _ = self._request(
            "PATCH",
            f"/experiments/{remote_id}",
            json_body=dict(payload),
            expected={200, 204},
        )
        if result is None:
            return {"id": remote_id}
        if not isinstance(result, dict):
            raise ELabFTWTransportError("eLabFTW update response is not a JSON object")
        return result

    def _request(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | None = None,
        expected: set[int],
    ) -> tuple[Any, dict[str, str]]:
        url = f"{self.endpoint}{path}"
        if query:
            encoded = urllib.parse.urlencode(
                [(key, value) for key, value in query.items() if value is not None],
                doseq=True,
            )
            if encoded:
                url = f"{url}?{encoded}"
        body = None
        headers = {
            "Accept": "application/json",
            "Authorization": self._api_key,
            "User-Agent": "mmfdb-elabftw/1",
        }
        if json_body is not None:
            body = json.dumps(json_body, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        response = self._transport(
            ELabFTWRequest(
                method=method,
                url=url,
                headers=headers,
                body=body,
                timeout=self.timeout,
                verify_tls=self.verify_tls,
                max_response_bytes=self.max_response_bytes,
            )
        )
        if len(response.body) > self.max_response_bytes:
            raise ELabFTWTransportError(
                f"eLabFTW response exceeds the {self.max_response_bytes}-byte safety limit"
            )
        normalized_headers = {
            str(key).lower(): str(value) for key, value in response.headers.items()
        }
        if int(response.status) not in expected:
            raise ELabFTWHTTPError(
                int(response.status), _error_message(response.body)
            )
        if not response.body:
            return None, normalized_headers
        try:
            return json.loads(response.body.decode("utf-8")), normalized_headers
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ELabFTWTransportError("eLabFTW returned malformed JSON") from exc


def _error_message(body: bytes) -> str:
    fallback = "request failed"
    if not body:
        return fallback
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return fallback
    if not isinstance(payload, dict):
        return fallback
    parts = [payload.get("message"), payload.get("description")]
    message = ": ".join(str(part) for part in parts if part)
    return message[:500] or fallback


def mmfdb_experiment_id(endpoint: str, remote_id: int) -> str:
    """Return a stable, instance-scoped MMFDB identifier for a remote entry."""
    remote_id = _positive_remote_id(remote_id)
    normalized = endpoint.rstrip("/").lower()
    instance = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    return f"elabftw:{instance}:{remote_id}"


def _metadata_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def remote_to_mmfdb_experiment(
    remote: Mapping[str, Any],
    *,
    endpoint: str,
    owner_user_id: str,
    type_id: int,
    existing: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Map one eLabFTW experiment onto MMFDB without discarding local details."""
    remote_id = _positive_remote_id(remote.get("id"))
    details: dict[str, Any] = {}
    if existing and existing.get("details"):
        try:
            loaded = json.loads(str(existing["details"]))
            if isinstance(loaded, dict):
                details.update(loaded)
        except json.JSONDecodeError:
            details["legacy_details"] = str(existing["details"])
    details["elabftw_source"] = {
        "provider": "elabftw",
        "endpoint": endpoint.rstrip("/"),
        "remote_id": remote_id,
        "url": f"{endpoint.rstrip('/')}/experiments/{remote_id}",
        "title": str(remote.get("title") or f"eLabFTW experiment {remote_id}"),
        "body": str(remote.get("body") or ""),
        "metadata": _metadata_value(remote.get("metadata")),
        "tags": list(remote.get("tags") or []),
        "modified_at": remote.get("modified_at") or remote.get("timestamped"),
    }
    return {
        "experiment_id": mmfdb_experiment_id(endpoint, remote_id),
        "type_id": int(type_id),
        "sample_id": existing.get("sample_id") if existing else None,
        "project_id": existing.get("project_id") if existing else None,
        "measured_by_user_id": (
            existing.get("measured_by_user_id") if existing else None
        )
        or owner_user_id,
        "measured_by_device_id": (
            existing.get("measured_by_device_id") if existing else None
        ),
        "started_at": remote.get("date") or remote.get("created_at"),
        "ended_at": existing.get("ended_at") if existing else None,
        "status": remote.get("status_title") or remote.get("status"),
        "details": json.dumps(details, sort_keys=True, ensure_ascii=False),
        "setup_definition_id": (
            existing.get("setup_definition_id") if existing else None
        ),
    }


def mmfdb_to_remote_experiment(local: Mapping[str, Any]) -> dict[str, Any]:
    """Map an MMFDB experiment to eLabFTW's editable experiment fields."""
    details: dict[str, Any] = {}
    raw_details = local.get("details")
    if raw_details:
        try:
            loaded = json.loads(str(raw_details))
            if isinstance(loaded, dict):
                details = loaded
        except json.JSONDecodeError:
            details = {"description": str(raw_details)}
    source = details.get("elabftw_source")
    if not isinstance(source, dict):
        source = {}
    title = str(
        source.get("title")
        or details.get("title")
        or local.get("experiment_id")
        or "MMFDB experiment"
    )
    body = source.get("body")
    if not body:
        description = details.get("description") or ""
        experiment_id = html.escape(str(local.get("experiment_id") or ""))
        body = (
            f"<p>{html.escape(str(description))}</p>"
            f"<p>MMFDB experiment: <code>{experiment_id}</code></p>"
        )
    payload: dict[str, Any] = {"title": title, "body": str(body)}
    started_at = str(local.get("started_at") or "")
    if started_at:
        payload["date"] = started_at[:10]
    tags = source.get("tags")
    if isinstance(tags, list) and tags:
        payload["tags"] = [str(tag) for tag in tags]
    return payload


__all__ = [
    "ELabFTWClient",
    "ELabFTWError",
    "ELabFTWHTTPError",
    "ELabFTWRequest",
    "ELabFTWResponse",
    "ELabFTWTransportError",
    "mmfdb_experiment_id",
    "mmfdb_to_remote_experiment",
    "remote_to_mmfdb_experiment",
]
