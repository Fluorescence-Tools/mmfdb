"""Bounded, redirect-free HTTP for ELN gateways, with an injectable transport.

Credentials ride in a header and must never follow a redirect to another
origin, so redirects are refused. A gateway takes a ``transport`` callable;
tests hand it recorded responses and no socket is opened.
"""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from mmfdb.eln.model import ElnUnavailable

MAX_RESPONSE_BYTES = 10 * 1024 * 1024


@dataclass(frozen=True)
class Request:
    """One request; the representation hides the credential."""

    method: str
    url: str
    headers: Mapping[str, str]
    body: bytes | None
    timeout: float
    verify_tls: bool

    def __repr__(self) -> str:
        """Return the request with the authorization header redacted."""
        safe = {k: "***" if k.lower() == "authorization" else v for k, v in self.headers.items()}
        return f"Request({self.method} {self.url} headers={safe} body_bytes={len(self.body or b'')})"


@dataclass(frozen=True)
class Response:
    """Transport-neutral response."""

    status: int
    headers: Mapping[str, str]
    body: bytes


Transport = Callable[[Request], Response]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def _read(stream: Any) -> bytes:
    data = stream.read(MAX_RESPONSE_BYTES + 1)
    if len(data) > MAX_RESPONSE_BYTES:
        raise ElnUnavailable(f"response exceeds the {MAX_RESPONSE_BYTES}-byte safety limit")
    return data


def urllib_transport(request: Request) -> Response:
    """Send *request* with the standard library."""
    req = urllib.request.Request(request.url, data=request.body, headers=dict(request.headers),
                                 method=request.method)
    handlers: list[Any] = [_NoRedirect()]
    if urllib.parse.urlsplit(request.url).scheme == "https":
        context = ssl.create_default_context() if request.verify_tls else ssl._create_unverified_context()  # noqa: SLF001
        handlers.append(urllib.request.HTTPSHandler(context=context))
    try:
        with urllib.request.build_opener(*handlers).open(req, timeout=request.timeout) as r:
            return Response(int(r.status), {k.lower(): v for k, v in r.headers.items()}, _read(r))
    except urllib.error.HTTPError as exc:
        try:
            return Response(int(exc.code), {k.lower(): v for k, v in exc.headers.items()}, _read(exc))
        finally:
            exc.close()
    except (OSError, urllib.error.URLError) as exc:
        raise ElnUnavailable(f"request failed: {getattr(exc, 'reason', exc)}") from exc


def multipart(fields: Mapping[str, str], files: Mapping[str, tuple[str, bytes, str]]) -> tuple[bytes, str]:
    """Encode a ``multipart/form-data`` body.

    Parameters
    ----------
    fields : mapping
        Text fields.
    files : mapping
        ``{field: (filename, data, media_type)}``.

    Returns
    -------
    tuple
        ``(body, content_type)``.
    """
    import hashlib

    parts: list[bytes] = []
    payload = b"".join(d for _, d, _ in files.values()) + json.dumps(dict(fields)).encode()
    boundary = "mmfdb" + hashlib.sha256(payload).hexdigest()[:24]
    for name, value in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    for name, (filename, data, media) in files.items():
        parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; '
                      f'filename="{filename}"\r\nContent-Type: {media}\r\n\r\n').encode() + data + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"
