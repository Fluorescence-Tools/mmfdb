"""Standalone typed ChiSurf PTO container transport, independent of ChiSurf runtime."""

from __future__ import annotations

import json
import tempfile
from collections.abc import Mapping
from pathlib import Path, PurePosixPath


def _safe_name(name: str) -> str:
    """Reject absolute paths, traversal and alternate separators without extraction."""
    if not name or '\\' in name or ':' in name or PurePosixPath(name).is_absolute() or '..' in PurePosixPath(name).parts:
        raise ValueError(f'Unsafe project entry path: {name!r}')
    return name


def _read(path: Path) -> dict[str, bytes]:
    """Read checked typed objects without interpreting client resource paths."""
    import tttrlib

    handle = tttrlib.PtoFile()
    if not handle.open(str(path)):
        raise ValueError(handle.error())
    try:
        if list(handle.verify()):
            raise ValueError('Invalid PTO container')
        tags = {tag.name: tag.text for tag in handle.tags_for(0)}
        if tags.get('chisurf.profile') != 'ChiSurf.Project' or tags.get('chisurf.profile_version') != '1':
            raise ValueError('Unsupported project PTO profile')
        objects = list(handle.objects())
        project = [obj for obj in objects if obj.kind == 'chisurf.project' and obj.name == 'project' and obj.encoding == 'json']
        if len(project) != 1:
            raise ValueError('Expected one typed project object')
        entries = {'project.json': bytes(handle.read(project[0].uid))}
        for obj in objects:
            if obj.kind == 'chisurf.project-entry':
                name = _safe_name(obj.name)
                if name in entries:
                    raise ValueError('Duplicate project entry')
                entries[name] = bytes(handle.read(obj.uid))
        return entries
    finally:
        handle.close()


def read_bundle(data: bytes) -> dict[str, bytes]:
    """Open supplied bytes in a server-owned temporary container only."""
    with tempfile.TemporaryDirectory(prefix='mmfdb-project-') as directory:
        path = Path(directory) / 'project.cs.pto'
        path.write_bytes(data)
        return _read(path)


def write_bundle(payload: dict, entries: Mapping[str, bytes]) -> bytes:
    """Write typed project state and attachments and verify exact readback."""
    import tttrlib

    project_bytes = json.dumps(payload, sort_keys=True, allow_nan=False).encode()
    expected = {'project.json': project_bytes, **{_safe_name(name): bytes(data) for name, data in entries.items()}}
    if 'project.json' in entries:
        raise ValueError('Duplicate project entry')
    with tempfile.TemporaryDirectory(prefix='mmfdb-project-') as directory:
        path = Path(directory) / 'project.cs.pto'
        handle = tttrlib.PtoFile()
        try:
            if not handle.create(str(path), 'Project export'):
                raise ValueError(handle.error())
            handle.set_writing_app('MMFDB')
            for name, value in (('chisurf.profile', 'ChiSurf.Project'), ('chisurf.profile_version', '1')):
                tag = tttrlib.PtoTag()
                tag.name, tag.type, tag.target, tag.text = name, tttrlib.PtoType_Text, 0, value
                handle.add_tag(tag)
            if not handle.add('chisurf.project', 'json', 'project', project_bytes):
                raise ValueError(handle.error())
            for name, data in entries.items():
                if not handle.add('chisurf.project-entry', 'raw', _safe_name(name), bytes(data)):
                    raise ValueError(handle.error())
            if not handle.commit():
                raise ValueError(handle.error())
        finally:
            handle.close()
        if _read(path) != expected:
            raise ValueError('PTO export readback mismatch')
        return path.read_bytes()
