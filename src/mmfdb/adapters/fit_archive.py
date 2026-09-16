"""Host-neutral fit archiving over MMFDB.

The node-graph adapter that backs this facade lives in
:mod:`mmfdb.adapters.chinet` (its name is historical: it stores a
node-graph session next to the fit state, and runs on chinet *or* on the
bff-backed compatibility runtime when chinet is absent). Hosts should
import from here so their import lines name what they mean -- archiving
a fit -- and not the runtime the storage happens to use.
"""
from __future__ import annotations

from mmfdb.adapters.chinet import archive_fit_to_mmfdb

__all__ = ["archive_fit_to_mmfdb"]
