# Standalone project snapshots

MMFDB exposes the existing `project_browser.save`, `project_browser.restore`,
and `project_browser.list` RPC names from its native service registry. A save
creates one immutable `operation_type = 'project'` version whose
`metadata_json.fit_structure` is the complete JSON project payload. Artifact
and provenance rows are indexes; restore reads the canonical payload and does
not attempt to rebuild a project from partial artifacts.

To extend a project, pass its `project_id` and `parent_version_id`. MMFDB
checks that the parent is readable and writable by the authenticated owner and
belongs to the same project before allocating the next version. New versions
inherit the default project branch. Public visibility changes the operation
ACL only after the snapshot is written in the same database transaction.

The standalone WSGI server resolves its database and object store from the
normal MMFDB runtime configuration. ChiSurf may use the same RPC methods over
HTTP, but MMFDB imports no ChiSurf modules at startup.
