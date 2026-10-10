"""ELN access tokens live in the OS credential store, never in a file.

Storage reuses :mod:`mmfdb.security.credentials`: the ``keyring`` package when it
is installed, the macOS Keychain through ``security`` otherwise. Where neither
exists (a headless Linux box without a secret service) the only supported source
is the environment variable ``MMFDB_<BACKEND>_TOKEN``, read at call time and never
persisted. There is deliberately no plaintext-file fallback.
"""

from __future__ import annotations

import os
import urllib.parse

from mmfdb.security import credentials as _store


def _account(backend: str, base_url: str) -> tuple[str, int]:
    return f"eln-{backend}-{urllib.parse.urlsplit(base_url).hostname or base_url}", 0


def store_token(backend: str, base_url: str, user: str, token: str) -> bool:
    """Store a token; ``False`` when no credential store accepted it."""
    host, port = _account(backend, base_url)
    return _store.store_session_token(host, port, user, token)


def load_token(backend: str, base_url: str, user: str) -> str | None:
    """Return the stored token, else ``MMFDB_<BACKEND>_TOKEN``, else None."""
    host, port = _account(backend, base_url)
    return _store.load_session_token(host, port, user) or os.environ.get(
        f"MMFDB_{backend.upper()}_TOKEN") or None


def delete_token(backend: str, base_url: str, user: str) -> bool:
    """Remove a stored token."""
    host, port = _account(backend, base_url)
    return _store.delete_session_token(host, port, user)
